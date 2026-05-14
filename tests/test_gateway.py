import unittest
import base64
import json
import os
import pathlib
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import src.toolcall_gateway as gateway
from src.toolcall_gateway import (
    BUILTIN_TOOLS,
    NativeToolVerificationError,
    ToolCall,
    _append_tool_results,
    _execute_tool_call,
    _extract_tool_calls,
    _is_forced_tool_choice,
    _mcp_list_server_tools,
    _mcp_public_name,
    _merge_builtin_tools,
    _native_tool_signal,
    _probe_body,
    _verify_native_if_forced,
    run_tool_orchestration,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def forward(self, path, body):
        self.requests.append((path, body))
        if not self.responses:
            raise AssertionError("no fake response left")
        return self.responses.pop(0)


class NativeGatewayTests(unittest.TestCase):
    def test_detects_chat_tool_calls(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "calculator", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        self.assertTrue(_native_tool_signal("/v1/chat/completions", response))

    def test_detects_responses_function_call(self):
        response = {"output": [{"type": "function_call", "name": "calculator", "arguments": "{}"}]}
        self.assertTrue(_native_tool_signal("/v1/responses", response))

    def test_detects_anthropic_tool_use(self):
        response = {"content": [{"type": "tool_use", "id": "toolu_1", "name": "calculator", "input": {}}]}
        self.assertTrue(_native_tool_signal("/v1/messages", response))

    def test_forced_chat_tool_choice(self):
        self.assertTrue(
            _is_forced_tool_choice(
                "/v1/chat/completions",
                {"tool_choice": {"type": "function", "function": {"name": "calculator"}}},
            )
        )
        self.assertFalse(_is_forced_tool_choice("/v1/chat/completions", {"tool_choice": "auto"}))

    def test_strict_forced_tool_choice_rejects_plain_answer(self):
        body = {
            "tools": [{"type": "function", "function": {"name": "calculator"}}],
            "tool_choice": {"type": "function", "function": {"name": "calculator"}},
        }
        response = {"choices": [{"message": {"role": "assistant", "content": "fake result"}}]}
        with self.assertRaises(NativeToolVerificationError):
            _verify_native_if_forced("/v1/chat/completions", body, response)

    def test_probe_body_uses_native_tool_choice_shapes(self):
        chat = _probe_body("/v1/chat/completions", "m")
        responses = _probe_body("/v1/responses", "m")
        messages = _probe_body("/v1/messages", "m")
        self.assertIn("tools", chat)
        self.assertEqual(chat["tool_choice"]["function"]["name"], "echo_probe")
        self.assertEqual(responses["tool_choice"]["name"], "echo_probe")
        self.assertEqual(messages["tool_choice"]["name"], "echo_probe")

    def test_builtin_registry_contains_common_coding_agent_tools(self):
        for name in [
            "Read",
            "Write",
            "Edit",
            "MultiEdit",
            "Bash",
            "Glob",
            "Grep",
            "LS",
            "WebFetch",
            "TodoWrite",
            "exec_command",
            "apply_patch",
            "list_mcp_resources",
        ]:
            self.assertIn(name, BUILTIN_TOOLS)

    def test_calculator_tool_executes(self):
        result = _execute_tool_call(
            ToolCall(call_id="call_1", name="calculator", arguments={"expression": "1+2*3"}, raw={})
        )
        self.assertTrue(result.success)
        self.assertEqual(result.content, "7")

    def test_read_glob_grep_tools_respect_workspace_root(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                with open(os.path.join(td, "a.txt"), "w", encoding="utf-8") as fh:
                    fh.write("alpha\nneedle\n")
                read = _execute_tool_call(ToolCall("r", "Read", {"file_path": "a.txt"}, {}))
                globbed = _execute_tool_call(ToolCall("g", "Glob", {"pattern": "*.txt"}, {}))
                grep = _execute_tool_call(ToolCall("p", "Grep", {"pattern": "needle"}, {}))
                self.assertIn("1: alpha", read.content)
                self.assertIn("a.txt", globbed.content)
                self.assertIn("a.txt:2: needle", grep.content)
            finally:
                if old is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old

    def test_extracts_and_appends_chat_tool_results(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "calculator", "arguments": "{\"expression\":\"2+2\"}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        calls = _extract_tool_calls("/v1/chat/completions", response)
        self.assertEqual(calls[0].name, "calculator")
        updated = _append_tool_results(
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "calc"}]},
            response,
            [_execute_tool_call(calls[0])],
        )
        self.assertEqual(updated["messages"][-1]["role"], "tool")
        self.assertEqual(updated["messages"][-1]["content"], "4")

    def test_orchestrates_chat_until_final(self):
        client = FakeClient(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "calculator",
                                            "arguments": "{\"expression\":\"9/3\"}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                {"choices": [{"message": {"role": "assistant", "content": "result is 3"}}]},
            ]
        )
        final = run_tool_orchestration(
            "/v1/chat/completions",
            {"model": "m", "messages": [{"role": "user", "content": "calc"}]},
            client,
        )
        self.assertEqual(final["choices"][0]["message"]["content"], "result is 3")
        self.assertEqual(client.requests[1][1]["messages"][-1]["content"], "3")

    def test_orchestrates_responses_until_final(self):
        client = FakeClient(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "calculator",
                            "arguments": "{\"expression\":\"5+5\"}",
                        }
                    ]
                },
                {"output": [{"type": "message", "content": [{"type": "output_text", "text": "10"}]}]},
            ]
        )
        final = run_tool_orchestration("/v1/responses", {"model": "m", "input": "calc"}, client)
        self.assertEqual(final["output"][0]["type"], "message")
        self.assertEqual(client.requests[1][1]["input"][-1]["output"], "10")

    def test_orchestrates_messages_until_final(self):
        client = FakeClient(
            [
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "calculator",
                            "input": {"expression": "4+4"},
                        }
                    ],
                    "stop_reason": "tool_use",
                },
                {"content": [{"type": "text", "text": "8"}]},
            ]
        )
        final = run_tool_orchestration(
            "/v1/messages",
            {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "calc"}]},
            client,
        )
        self.assertEqual(final["content"][0]["text"], "8")
        self.assertEqual(client.requests[1][1]["messages"][-1]["content"][0]["content"], "8")

    def test_mcp_stdio_tools_list_call_and_schema_merge(self):
        script = r'''
import json, sys
def read_msg():
    header = b""
    while b"\r\n\r\n" not in header:
        b = sys.stdin.buffer.read(1)
        if not b:
            return None
        header += b
    length = 0
    for line in header.decode().splitlines():
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    return json.loads(sys.stdin.buffer.read(length).decode())
def write_msg(msg):
    raw = json.dumps(msg).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    sys.stdout.buffer.flush()
while True:
    msg = read_msg()
    if msg is None:
        break
    method = msg.get("method")
    if "id" not in msg:
        continue
    if method == "initialize":
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"1"}}})
    elif method == "tools/list":
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"tools":[{"name":"echo_mcp","description":"Echo via MCP","inputSchema":{"type":"object","properties":{"value":{"type":"string"}},"required":["value"]}}]}})
    elif method == "tools/call":
        value = msg.get("params", {}).get("arguments", {}).get("value", "")
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"content":[{"type":"text","text":"mcp:" + value}]}})
    else:
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{}})
'''
        with tempfile.TemporaryDirectory() as td:
            script_path = pathlib.Path(td) / "fake_mcp.py"
            script_path.write_text(script, encoding="utf-8")
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                gateway.save_config(
                    {
                        **gateway._default_config(),
                        "mcp": {
                            "enabled": True,
                            "servers": [
                                {
                                    "name": "test",
                                    "command": sys.executable,
                                    "args": [str(script_path)],
                                    "cwd": td,
                                    "enabled": True,
                                }
                            ],
                        },
                    }
                )
                server = gateway.load_config()["mcp"]["servers"][0]
                tools = _mcp_list_server_tools(server)
                self.assertEqual(tools[0]["name"], "echo_mcp")
                tools_again = _mcp_list_server_tools(server)
                self.assertEqual(tools_again[0]["name"], "echo_mcp")
                self.assertEqual(len(gateway.MCP_SESSIONS), 1)
                self.assertEqual(len(gateway.MCP_TOOL_CATALOG_CACHE), 1)
                public_name = _mcp_public_name("test", "echo_mcp")
                result = _execute_tool_call(ToolCall("mcp1", public_name, {"value": "ok"}, {}))
                self.assertTrue(result.success)
                self.assertEqual(result.content, "mcp:ok")
                merged = _merge_builtin_tools("/v1/chat/completions", {"messages": []})
                names = [
                    t.get("function", {}).get("name")
                    for t in merged["tools"]
                    if isinstance(t, dict) and isinstance(t.get("function"), dict)
                ]
                self.assertIn(public_name, names)
            finally:
                gateway._mcp_close_sessions()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_mcp_broken_server_marks_health_and_invalidates_cache(self):
        script = "import sys\nsys.exit(0)\n"
        with tempfile.TemporaryDirectory() as td:
            script_path = pathlib.Path(td) / "broken_mcp.py"
            script_path.write_text(script, encoding="utf-8")
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                gateway.save_config(
                    {
                        **gateway._default_config(),
                        "mcp": {
                            "enabled": True,
                            "servers": [
                                {
                                    "name": "broken",
                                    "command": sys.executable,
                                    "args": [str(script_path)],
                                    "cwd": td,
                                    "enabled": True,
                                }
                            ],
                        },
                    }
                )
                server = gateway.load_config()["mcp"]["servers"][0]
                with self.assertRaises(Exception):
                    _mcp_list_server_tools(server)
                self.assertEqual(gateway.MCP_SERVER_STATUS["broken"]["status"], "broken")
                self.assertEqual(gateway.MCP_SERVER_STATUS["broken"]["tool_count"], 0)
                self.assertNotIn("broken", gateway.MCP_SESSIONS)
                self.assertNotIn("broken", gateway.MCP_TOOL_CATALOG_CACHE)
                health = gateway._mcp_health_snapshot(probe=False)
                self.assertEqual(health[0]["name"], "broken")
                self.assertEqual(health[0]["status"], "broken")
                self.assertEqual(health[0]["session"], "not_connected")
                self.assertEqual(health[0]["cache"], "miss")
            finally:
                gateway._mcp_close_sessions()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_admin_mcp_health_endpoint_supports_probe_query(self):
        script = "import sys\nsys.exit(0)\n"
        with tempfile.TemporaryDirectory() as td:
            script_path = pathlib.Path(td) / "broken_mcp.py"
            script_path.write_text(script, encoding="utf-8")
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            httpd = None
            thread = None
            try:
                gateway.save_config(
                    {
                        **gateway._default_config(),
                        "mcp": {
                            "enabled": True,
                            "servers": [
                                {
                                    "name": "broken",
                                    "command": sys.executable,
                                    "args": [str(script_path)],
                                    "cwd": td,
                                    "enabled": True,
                                }
                            ],
                        },
                    }
                )
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                token = base64.b64encode(b"admin:admin").decode("ascii")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/mcp-health.json?probe=1",
                    headers={"authorization": f"Basic {token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(payload["servers"][0]["name"], "broken")
                self.assertEqual(payload["servers"][0]["status"], "broken")
                self.assertEqual(payload["servers"][0]["session"], "not_connected")
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway._mcp_close_sessions()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_http_action_exposes_schema_and_executes_real_http(self):
        class EchoHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                payload = json.dumps({"received": body}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                gateway.save_config(
                    {
                        **gateway._default_config(),
                        "http_actions": {
                            "enabled": True,
                            "actions": [
                                {
                                    "name": "echo_http",
                                    "description": "Echo through real HTTP action",
                                    "method": "POST",
                                    "url": f"http://127.0.0.1:{httpd.server_address[1]}/echo",
                                    "input_schema": {
                                        "type": "object",
                                        "properties": {"value": {"type": "string"}},
                                        "required": ["value"],
                                    },
                                }
                            ],
                        },
                    }
                )
                merged = _merge_builtin_tools("/v1/chat/completions", {"messages": []})
                names = [
                    t.get("function", {}).get("name")
                    for t in merged["tools"]
                    if isinstance(t, dict) and isinstance(t.get("function"), dict)
                ]
                self.assertIn("echo_http", names)
                result = _execute_tool_call(ToolCall("http1", "echo_http", {"value": "ok"}, {}))
                self.assertTrue(result.success)
                body = json.loads(result.content.split("\n\n", 1)[1])
                self.assertEqual(body["received"]["value"], "ok")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway._mcp_close_sessions()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root


if __name__ == "__main__":
    unittest.main()
