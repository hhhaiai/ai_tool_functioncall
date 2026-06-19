import unittest
import base64
import json
import os
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
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
    _mcp_legacy_public_name,
    _mcp_parse_public_name,
    _mcp_public_name,
    _merge_builtin_tools,
    _native_tool_signal,
    _probe_body,
    _parse_text_tool_calls,
    _response_text,
    _verify_native_if_forced,
    execute_direct_tool_call,
    run_tool_orchestration,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.lock = threading.Lock()

    def forward(self, path, body):
        with self.lock:
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

    def test_anthropic_stream_tool_use_start_input_is_replayed_as_delta(self):
        from src.gateway_streaming import _normalize_anthropic_sse_block

        raw = (
            b"event: content_block_start\n"
            b'data: {"type":"content_block_start","index":1,'
            b'"content_block":{"type":"tool_use","id":"call_1","name":"Bash",'
            b'"input":{"command":"printf ok"}}}\n\n'
        )

        blocks = _normalize_anthropic_sse_block(raw)

        self.assertEqual(len(blocks), 2)
        self.assertIn(b"event: content_block_start", blocks[0])
        self.assertIn(b'"input": {}', blocks[0])
        self.assertIn(b"event: content_block_delta", blocks[1])
        self.assertIn(b'"type": "input_json_delta"', blocks[1])
        self.assertIn(b'\\"command\\": \\"printf ok\\"', blocks[1])

    def test_anthropic_stream_text_blocks_are_not_rewritten(self):
        from src.gateway_streaming import _normalize_anthropic_sse_block

        raw = (
            b"event: content_block_start\n"
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
        )

        self.assertEqual(_normalize_anthropic_sse_block(raw), [raw])

    def test_calculator_tool_executes(self):
        result = _execute_tool_call(
            ToolCall(call_id="call_1", name="calculator", arguments={"expression": "1+2*3"}, raw={})
        )
        self.assertTrue(result.success)
        self.assertEqual(result.content, "7")

    def test_calculator_accepts_user_calc_tool_alias_and_expr_argument(self):
        result = _execute_tool_call(
            ToolCall(call_id="call_1", name="calc", arguments={"expr": "2+2"}, raw={})
        )
        self.assertTrue(result.success)
        self.assertEqual(result.content, "4")

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

    def test_workspace_tools_reject_paths_outside_workspace_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "workspace"
            root.mkdir()
            outside = pathlib.Path(td) / "outside.txt"
            outside.write_text("secret\n", encoding="utf-8")
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(root)
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(root)
                cfg["gateway"]["allow_write_tools"] = True
                gateway.save_config(cfg)

                relative_escape = _execute_tool_call(
                    ToolCall("escape_read", "Read", {"file_path": "../outside.txt"}, {})
                )
                absolute_escape = _execute_tool_call(
                    ToolCall("escape_abs", "Read", {"file_path": str(outside)}, {})
                )
                write_escape = _execute_tool_call(
                    ToolCall("escape_write", "Write", {"file_path": "../outside.txt", "content": "changed"}, {})
                )

                self.assertFalse(relative_escape.success)
                self.assertFalse(absolute_escape.success)
                self.assertFalse(write_escape.success)
                self.assertEqual(relative_escape.failure_type, "permission_denied")
                self.assertEqual(absolute_escape.failure_type, "permission_denied")
                self.assertEqual(write_escape.failure_type, "permission_denied")
                self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_delete_path_refuses_workspace_root_even_recursive(self):
        with tempfile.TemporaryDirectory() as td:
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = td
                cfg["gateway"]["allow_write_tools"] = True
                gateway.save_config(cfg)
                result = _execute_tool_call(ToolCall("delroot", "DeletePath", {"path": ".", "recursive": True}, {}))
                self.assertFalse(result.success)
                self.assertEqual(result.failure_type, "permission_denied")
                self.assertTrue(pathlib.Path(td).exists())
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_read_long_file_is_chunked_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_limit = os.environ.get("GATEWAY_READ_DEFAULT_LIMIT")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_READ_DEFAULT_LIMIT"] = "3"
            try:
                with open(os.path.join(td, "huge.py"), "w", encoding="utf-8") as fh:
                    fh.write("\n".join(f"line {i}" for i in range(1, 8)))
                read = _execute_tool_call(ToolCall("r", "Read", {"file_path": "huge.py"}, {}))
                self.assertTrue(read.success)
                self.assertIn("1: line 1", read.content)
                self.assertIn("3: line 3", read.content)
                self.assertNotIn("4: line 4", read.content)
                self.assertIn("offset=4", read.content)
            finally:
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_limit is None:
                    os.environ.pop("GATEWAY_READ_DEFAULT_LIMIT", None)
                else:
                    os.environ["GATEWAY_READ_DEFAULT_LIMIT"] = old_limit

    def test_alias_and_argument_normalization_prevents_invalid_parameters(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                with open(os.path.join(td, "a.txt"), "w", encoding="utf-8") as fh:
                    fh.write("alpha\n")
                read = _execute_tool_call(ToolCall("r", "view", {"file": "a.txt"}, {}))
                self.assertTrue(read.success)
                self.assertIn("alpha", read.content)
                calc = _execute_tool_call(ToolCall("c", "calculator", {"text": "6*7"}, {}))
                self.assertEqual(calc.content, "42")
            finally:
                if old is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old

    def test_downstream_api_key_env_creates_auth_key_and_snippet(self):
        old_downstream = os.environ.get("DOWNSTREAM_API_KEY")
        old_gateway = os.environ.get("GATEWAY_DOWNSTREAM_KEY")
        try:
            os.environ.pop("GATEWAY_DOWNSTREAM_KEY", None)
            os.environ["DOWNSTREAM_API_KEY"] = "env-downstream-key"
            cfg = gateway._default_config()
            self.assertEqual(cfg["gateway"]["client_snippet_api_key"], "env-downstream-key")
            self.assertEqual(len(cfg["downstream_keys"]), 1)
            self.assertEqual(cfg["downstream_keys"][0]["prefix"], "env-down")

            os.environ["GATEWAY_DOWNSTREAM_KEY"] = "gateway-key"
            cfg = gateway._default_config()
            self.assertEqual(cfg["gateway"]["client_snippet_api_key"], "env-downstream-key")
            self.assertEqual(cfg["downstream_keys"][0]["prefix"], "gateway-")
        finally:
            if old_downstream is None:
                os.environ.pop("DOWNSTREAM_API_KEY", None)
            else:
                os.environ["DOWNSTREAM_API_KEY"] = old_downstream
            if old_gateway is None:
                os.environ.pop("GATEWAY_DOWNSTREAM_KEY", None)
            else:
                os.environ["GATEWAY_DOWNSTREAM_KEY"] = old_gateway

    def test_admin_plain_password_config_is_hashed_and_not_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                gateway.CONFIG_PATH.write_text(
                    json.dumps(
                        {
                            "admin": {"username": "admin", "password": "configured-admin-pass"},
                            "upstream": {"base_url": "http://upstream.local", "model": "m"},
                        }
                    ),
                    encoding="utf-8",
                )

                cfg = gateway.load_config()
                self.assertEqual(cfg["admin"]["password_hash"], gateway._hash_secret("configured-admin-pass"))
                self.assertNotIn("password", cfg["admin"])

                gateway.save_config(cfg)
                saved = json.loads(gateway.CONFIG_PATH.read_text(encoding="utf-8"))
                self.assertEqual(saved["admin"]["password_hash"], gateway._hash_secret("configured-admin-pass"))
                self.assertNotIn("password", saved["admin"])
                self.assertNotIn("configured-admin-pass", gateway.CONFIG_PATH.read_text(encoding="utf-8"))
            finally:
                gateway.CONFIG_PATH = old_config

    def test_admin_password_hash_takes_precedence_over_plain_password(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                expected_hash = gateway._hash_secret("hash-wins")
                gateway.CONFIG_PATH.write_text(
                    json.dumps(
                        {
                            "admin": {
                                "username": "admin",
                                "password": "ignored-plain-password",
                                "password_hash": expected_hash,
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                cfg = gateway.load_config()
                self.assertEqual(cfg["admin"]["password_hash"], expected_hash)
                self.assertNotIn("password", cfg["admin"])
            finally:
                gateway.CONFIG_PATH = old_config

    def test_invalid_config_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                gateway.CONFIG_PATH.write_text("{not valid json", encoding="utf-8")
                with self.assertRaises(gateway.ConfigError) as json_error:
                    gateway.load_config()
                self.assertIn("invalid gateway config", str(json_error.exception))
                self.assertIn("JSONDecodeError", str(json_error.exception.detail))

                gateway.CONFIG_PATH.write_text("[]", encoding="utf-8")
                with self.assertRaises(gateway.ConfigError) as root_error:
                    gateway.load_config()
                self.assertIn("config root must be object", str(root_error.exception.detail))
            finally:
                gateway.CONFIG_PATH = old_config

    def test_invalid_config_returns_structured_error_for_admin_and_api(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            gateway.CONFIG_PATH.write_text("{not valid json", encoding="utf-8")
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                token = base64.b64encode(b"admin:admin").decode("ascii")

                ui_req = urllib.request.Request(base + "/ui", headers={"authorization": f"Basic {token}"})
                with self.assertRaises(urllib.error.HTTPError) as ui_error:
                    urllib.request.urlopen(ui_req, timeout=5)
                self.assertEqual(ui_error.exception.code, 500)
                ui_payload = json.loads(ui_error.exception.read().decode("utf-8"))
                self.assertIn("invalid gateway config", ui_payload["error"]["message"])
                self.assertIn("JSONDecodeError", ui_payload["error"]["detail"])

                tool_req = urllib.request.Request(
                    base + "/v1/tools/call",
                    data=json.dumps({"tool": "calculator", "arguments": {"expression": "6*7"}}).encode("utf-8"),
                    headers={"authorization": "Bearer local-gateway-key", "content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as tool_error:
                    urllib.request.urlopen(tool_req, timeout=5)
                self.assertEqual(tool_error.exception.code, 500)
                tool_payload = json.loads(tool_error.exception.read().decode("utf-8"))
                self.assertIn("invalid gateway config", tool_payload["error"]["message"])
                self.assertIn("JSONDecodeError", tool_payload["error"]["detail"])
                self.assertNotIn("success", tool_payload)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_client_snippet_key_is_normalized_into_downstream_auth(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                gateway.CONFIG_PATH.write_text(
                    json.dumps({"gateway": {"client_snippet_api_key": "snippet-only-key"}, "downstream_keys": []}),
                    encoding="utf-8",
                )
                cfg = gateway.load_config()
                self.assertTrue(
                    any(
                        isinstance(item, dict) and item.get("key_hash") == gateway._hash_secret("snippet-only-key")
                        for item in cfg["downstream_keys"]
                    )
                )
                gateway.save_config(cfg)
                saved = json.loads(gateway.CONFIG_PATH.read_text(encoding="utf-8"))
                snippet_entries = [item for item in saved["downstream_keys"] if item.get("name") == "client-snippet"]
                self.assertEqual(len(snippet_entries), 1)
                self.assertEqual(snippet_entries[0]["key_hash"], gateway._hash_secret("snippet-only-key"))
                self.assertNotIn("key", snippet_entries[0])

                gateway.CONFIG_PATH.write_text(
                    json.dumps(
                        {
                            "gateway": {"client_snippet_api_key": "disabled-existing-key"},
                            "downstream_keys": [
                                {
                                    "name": "old-disabled",
                                    "key_hash": gateway._hash_secret("disabled-existing-key"),
                                    "prefix": "",
                                    "enabled": False,
                                    "protocols": ["models"],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                repaired = gateway.load_config()["downstream_keys"][0]
                self.assertTrue(repaired["enabled"])
                self.assertEqual(repaired["prefix"], "disabled")
                self.assertEqual(repaired["protocols"], ["models", "chat_completions", "responses", "messages", "direct_tools"])
            finally:
                gateway.CONFIG_PATH = old_config

    def test_public_runtime_templates_keep_safe_defaults(self):
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        template = json.loads((repo_root / "gateway.config.json").read_text(encoding="utf-8"))
        self.assertEqual(template["gateway"]["workspace_root"], "./workspace")
        self.assertFalse(template["gateway"]["allow_write_tools"])
        self.assertFalse(template["gateway"]["allow_shell_tools"])
        self.assertEqual(template["gateway"].get("max_request_body_bytes"), 64 * 1024 * 1024)
        self.assertEqual(template["gateway"].get("max_log_payload_chars"), 200000)

        yaml_text = (repo_root / "gateway.config.yaml").read_text(encoding="utf-8")
        self.assertIn("workspace_root: ./workspace", yaml_text)
        self.assertIn("allow_write_tools: false", yaml_text)
        self.assertIn("allow_shell_tools: false", yaml_text)
        self.assertIn("max_request_body_bytes: 67108864", yaml_text)
        self.assertIn("max_log_payload_chars: 200000", yaml_text)

        env_example = (repo_root / ".env.example").read_text(encoding="utf-8")
        dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
        compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        prod_compose = (repo_root / "docker-compose.prod.yml").read_text(encoding="utf-8")
        self.assertNotIn("GATEWAY_ADMIN_PASSWORD=admin", dockerfile)
        self.assertIn("GATEWAY_MAX_REQUEST_BODY_BYTES=67108864", env_example)
        self.assertIn("GATEWAY_MAX_LOG_PAYLOAD_CHARS=200000", env_example)
        self.assertIn("GATEWAY_MAX_REQUEST_BODY_BYTES=${GATEWAY_MAX_REQUEST_BODY_BYTES:-67108864}", compose)
        self.assertIn("GATEWAY_MAX_LOG_PAYLOAD_CHARS=${GATEWAY_MAX_LOG_PAYLOAD_CHARS:-200000}", compose)
        self.assertIn("GATEWAY_MAX_REQUEST_BODY_BYTES=${GATEWAY_MAX_REQUEST_BODY_BYTES:-67108864}", prod_compose)
        self.assertIn("GATEWAY_MAX_LOG_PAYLOAD_CHARS=${GATEWAY_MAX_LOG_PAYLOAD_CHARS:-200000}", prod_compose)
        self.assertIn("GATEWAY_ADMIN_PASSWORD=${GATEWAY_ADMIN_PASSWORD:-}", compose)
        self.assertIn("GATEWAY_ADMIN_PASSWORD=${GATEWAY_ADMIN_PASSWORD:?set GATEWAY_ADMIN_PASSWORD}", prod_compose)

    def test_upstream_protocol_env_supports_current_and_legacy_names(self):
        old_current = os.environ.get("GATEWAY_UPSTREAM_PROTOCOL")
        old_legacy = os.environ.get("UPSTREAM_PROTOCOL")
        try:
            os.environ.pop("GATEWAY_UPSTREAM_PROTOCOL", None)
            os.environ["UPSTREAM_PROTOCOL"] = "anthropic_messages"
            self.assertEqual(gateway._env_upstream_protocol(), "anthropic_messages")
            self.assertEqual(gateway._default_config()["upstream"]["protocol"], "anthropic_messages")

            os.environ["GATEWAY_UPSTREAM_PROTOCOL"] = "openai_responses"
            self.assertEqual(gateway._env_upstream_protocol(), "openai_responses")
            self.assertEqual(gateway._default_config()["upstream"]["protocol"], "openai_responses")
        finally:
            if old_current is None:
                os.environ.pop("GATEWAY_UPSTREAM_PROTOCOL", None)
            else:
                os.environ["GATEWAY_UPSTREAM_PROTOCOL"] = old_current
            if old_legacy is None:
                os.environ.pop("UPSTREAM_PROTOCOL", None)
            else:
                os.environ["UPSTREAM_PROTOCOL"] = old_legacy

    def test_direct_tool_call_accepts_openai_function_shape(self):
        result = execute_direct_tool_call(
            {"function": {"name": "calculator", "arguments": "{\"expression\":\"20+22\"}"}, "call_id": "call_direct"}
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "42")
        self.assertEqual(result["openai_chat"]["tool_call_id"], "call_direct")

    def test_direct_tool_call_accepts_tool_alias_shape(self):
        result = execute_direct_tool_call({"tool": "calculator", "arguments": {"expression": "6*7"}, "call_id": "call_tool_alias"})
        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "42")
        self.assertEqual(result["openai_chat"]["tool_call_id"], "call_tool_alias")

    def test_gateway_internal_workspace_fields_are_not_forwarded_upstream(self):
        from src.gateway_protocol import _convert_request_to_upstream

        body = {
            "model": "m",
            "workspace_root": "/tmp/secret-project",
            "gateway_workspace": "/tmp/secret-project",
            "projectDir": "/tmp/secret-project",
            "cwd": "/tmp/secret-project",
            "metadata": {
                "session_id": "s1",
                "workspace_root": "/tmp/secret-project",
                "gateway_workspace": "/tmp/secret-project",
                "projectDir": "/tmp/secret-project",
                "user_id": json.dumps({"session_id": "s1", "cwd": "/tmp/secret-project"}),
            },
            "messages": [{"role": "user", "content": "hello"}],
        }

        _, converted = _convert_request_to_upstream("/v1/chat/completions", body, "openai_chat")

        serialized = json.dumps(converted, ensure_ascii=False)
        self.assertNotIn("/tmp/secret-project", serialized)
        self.assertNotIn("workspace_root", converted)
        self.assertNotIn("gateway_workspace", converted)
        self.assertEqual(converted["metadata"]["session_id"], "s1")
        self.assertEqual(json.loads(converted["metadata"]["user_id"]), {"session_id": "s1"})

        _, converted_string_metadata = _convert_request_to_upstream(
            "/v1/chat/completions",
            {
                "model": "m",
                "metadata": json.dumps({"session_id": "s2", "workspace_root": "/tmp/secret-project"}),
                "messages": [{"role": "user", "content": "hello"}],
            },
            "openai_chat",
        )
        self.assertEqual(converted_string_metadata["metadata"], {"session_id": "s2"})

    def test_streaming_passthrough_strips_internal_workspace_fields(self):
        from src.gateway_streaming import _stream_upstream_passthrough

        class StreamingUpstreamHandler(BaseHTTPRequestHandler):
            seen_body = None

            def log_message(self, fmt, *args):  # noqa: N802
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or "0")
                StreamingUpstreamHandler.seen_body = json.loads(self.rfile.read(length).decode("utf-8"))
                payload = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        class DummyHandler:
            def __init__(self):
                self.headers = []
                self.wfile = self
                self.data = bytearray()

            def send_response(self, code):
                self.code = code

            def send_header(self, name, value):
                self.headers.append((name, value))

            def end_headers(self):
                pass

            def write(self, data):
                self.data.extend(data)

            def flush(self):
                pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), StreamingUpstreamHandler)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
                cfg["upstream"]["model"] = "m"
                cfg["upstream"]["protocol"] = "openai_chat"
                gateway.save_config(cfg)
                handler = DummyHandler()
                _stream_upstream_passthrough(
                    handler,
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "stream": True,
                        "workspace_root": "/tmp/secret-project",
                        "gateway_workspace": "/tmp/secret-project",
                        "metadata": {"session_id": "s1", "workspace_root": "/tmp/secret-project", "user_id": json.dumps({"session_id": "s1", "cwd": "/tmp/secret-project"})},
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                serialized = json.dumps(StreamingUpstreamHandler.seen_body, ensure_ascii=False)
                self.assertNotIn("/tmp/secret-project", serialized)
                self.assertEqual(StreamingUpstreamHandler.seen_body["metadata"]["session_id"], "s1")
                self.assertEqual(json.loads(StreamingUpstreamHandler.seen_body["metadata"]["user_id"]), {"session_id": "s1"})
                self.assertIn(b"data: [DONE]", bytes(handler.data))
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_direct_tool_call_can_scope_workspace_per_request(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = td
                gateway.save_config(cfg)
                pathlib.Path(other, "app.py").write_text("print('other')\n", encoding="utf-8")
                result = execute_direct_tool_call(
                    {"workspace_root": other, "tool": "Read", "arguments": {"file_path": "app.py"}, "call_id": "scoped"}
                )
                self.assertTrue(result["success"])
                self.assertIn("other", result["content"])
            finally:
                gateway.CONFIG_PATH = old_config

    def test_direct_tool_call_http_endpoint_is_callable(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_force = os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = "0"
            gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            gateway_thread.start()
            try:
                gateway.save_config(gateway._default_config())
                req = urllib.request.Request(
                    f"http://127.0.0.1:{gateway_server.server_address[1]}/v1/tools/call",
                    data=json.dumps(
                        {"function": {"name": "calculator", "arguments": "{\"expression\":\"20+22\"}"}, "call_id": "http_call"}
                    ).encode("utf-8"),
                    headers={"authorization": "Bearer local-gateway-key", "content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(payload["success"])
                self.assertEqual(payload["content"], "42")
                self.assertEqual(payload["openai_responses"]["call_id"], "http_call")
            finally:
                gateway_server.shutdown()
                gateway_server.server_close()
                gateway_thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_force is None:
                    os.environ.pop("GATEWAY_UPSTREAM_STREAM_AGGREGATE", None)
                else:
                    os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = old_force

    def test_http_api_enforces_configured_concurrency_limit_before_work(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            slot = None
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["client_snippet_api_key"] = "slot-key"
                cfg["gateway"]["max_concurrent_requests"] = 1
                cfg["gateway"]["concurrency_queue_timeout_seconds"] = 0.01
                gateway.save_config(cfg)
                slot = gateway._acquire_request_slot()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/v1/tools/call",
                    data=json.dumps({"tool": "calculator", "arguments": {"expression": "2+2"}}).encode("utf-8"),
                    headers={"authorization": "Bearer slot-key", "content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(err.exception.code, 429)
                payload = json.loads(err.exception.read().decode("utf-8"))
                self.assertIn("gateway concurrency limit reached (1)", payload["error"]["message"])
            finally:
                if slot is not None:
                    slot.release()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_direct_tool_call_accepts_anthropic_tool_use_shape(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                with open(os.path.join(td, "note.txt"), "w", encoding="utf-8") as fh:
                    fh.write("hello\n")
                result = execute_direct_tool_call(
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file": "note.txt"}}
                )
                self.assertTrue(result["success"])
                self.assertEqual(result["anthropic"]["tool_use_id"], "toolu_1")
                self.assertIn("hello", result["content"])
            finally:
                if old is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old

    def test_code_interpreter_is_real_but_permission_gated(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["allow_shell_tools"] = False
                gateway.save_config(cfg)
                result = execute_direct_tool_call({"name": "code_interpreter", "arguments": {"code": "print(40+2)"}})
                self.assertFalse(result["success"])
                self.assertEqual(result["failure_type"], "permission_denied")
            finally:
                gateway.CONFIG_PATH = old_config

    def test_multi_tool_use_parallel_executes_nested_gateway_tools(self):
        result = _execute_tool_call(
            ToolCall(
                "parallel",
                "multi_tool_use.parallel",
                {
                    "tool_uses": [
                        {"recipient_name": "functions.calculator", "parameters": {"expression": "1+1"}},
                        {"recipient_name": "functions.calculator", "parameters": {"text": "5*5"}},
                    ]
                },
                {},
            )
        )
        self.assertTrue(result.success)
        payload = json.loads(result.content)
        self.assertEqual(payload["results"][0]["content"], "2")
        self.assertEqual(payload["results"][1]["content"], "25")

    def test_more_tool_compat_tree_json_symbols_and_catalog(self):
        with tempfile.TemporaryDirectory() as td:
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_config = gateway.CONFIG_PATH
            old_failure_log = os.environ.get("GATEWAY_TOOL_FAILURE_LOG")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_TOOL_FAILURE_LOG"] = str(pathlib.Path(td) / "failures.jsonl")
            try:
                gateway.save_config(gateway._default_config())
                pathlib.Path(td, "pkg").mkdir()
                pathlib.Path(td, "pkg", "mod.py").write_text("import os\nclass A:\n    def f(self):\n        return 1\n", encoding="utf-8")
                pathlib.Path(td, "data.json").write_text('{"a":{"b":[1,2,3]}}', encoding="utf-8")
                tree = _execute_tool_call(ToolCall("tree", "tree", {"path": "."}, {}))
                self.assertTrue(tree.success)
                self.assertIn("pkg/", tree.content)
                symbols = _execute_tool_call(ToolCall("sym", "python_symbols", {"file_path": "pkg/mod.py"}, {}))
                self.assertTrue(symbols.success)
                self.assertIn('"name": "A"', symbols.content)
                query = _execute_tool_call(ToolCall("jq", "jq", {"file_path": "data.json", "query": "a.b.1"}, {}))
                self.assertTrue(query.success)
                self.assertEqual(query.content.strip(), "2")
                catalog = gateway._tool_catalog_snapshot()
                names = {tool["name"] for tool in catalog["tools"]}
                self.assertIn("Agent", names)
                self.assertIn("Skill", names)
                self.assertIn("Tree", names)
                missing = _execute_tool_call(ToolCall("missing", "not_installed_tool", {}, {}))
                self.assertFalse(missing.success)
                catalog = gateway._tool_catalog_snapshot()
                failed_names = {row["tool"] for row in catalog["unsupported_or_failed"]}
                self.assertIn("not_installed_tool", failed_names)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_failure_log is None:
                    os.environ.pop("GATEWAY_TOOL_FAILURE_LOG", None)
                else:
                    os.environ["GATEWAY_TOOL_FAILURE_LOG"] = old_failure_log

    def test_skill_tool_discovers_project_scoped_codex_claude_opencode_plugin_and_env_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            old_cwd = pathlib.Path.cwd()
            old_extra = os.environ.get("GATEWAY_SKILLS_DIRS")
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            try:
                for rel, marker in [
                    ("workspace/.codex/skills/codex-skill/SKILL.md", "from-codex"),
                    ("workspace/.agents/skills/agents-skill/SKILL.md", "from-agents"),
                    ("workspace/.claude/skills/claude-skill/SKILL.md", "from-claude"),
                    ("workspace/.opencode/skills/opencode-skill/SKILL.md", "from-opencode"),
                    ("workspace/skills/workspace-skill/SKILL.md", "from-workspace-skills"),
                    ("extra-skills/env-skill/SKILL.md", "from-env-skills"),
                    (".claude/skills/service-cwd-skill/SKILL.md", "from-service-cwd"),
                ]:
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"# {path.parent.name}\n\n{marker}\n", encoding="utf-8")
                plugin_manifest = workspace / ".codex/plugins/demo/.codex-plugin/plugin.json"
                plugin_manifest.parent.mkdir(parents=True, exist_ok=True)
                plugin_manifest.write_text(json.dumps({"name": "demo", "skills": "./skills"}), encoding="utf-8")
                plugin_skill = workspace / ".codex/plugins/demo/skills/plugin-skill/SKILL.md"
                plugin_skill.parent.mkdir(parents=True, exist_ok=True)
                plugin_skill.write_text("# plugin-skill\n\nfrom-project-plugin\n", encoding="utf-8")
                os.environ["GATEWAY_SKILLS_DIRS"] = str(root / "extra-skills")
                os.environ["GATEWAY_WORKSPACE_ROOT"] = str(workspace)
                os.chdir(root)

                listed = _execute_tool_call(ToolCall("skills", "list_skills", {}, {}))
                self.assertTrue(listed.success)
                payload = json.loads(listed.content)
                names = {item["name"] for item in payload["skills"]}
                self.assertIn("codex-skill", names)
                self.assertIn("agents-skill", names)
                self.assertIn("claude-skill", names)
                self.assertIn("opencode-skill", names)
                self.assertIn("workspace-skill", names)
                self.assertIn("plugin-skill", names)
                self.assertIn("env-skill", names)
                self.assertNotIn("service-cwd-skill", names)

                read = _execute_tool_call(ToolCall("read-skill", "read_skill", {"name": "claude-skill"}, {}))
                self.assertTrue(read.success)
                self.assertIn("from-claude", read.content)
                plugin_read = _execute_tool_call(ToolCall("read-plugin-skill", "read_skill", {"name": "plugin-skill"}, {}))
                self.assertTrue(plugin_read.success)
                self.assertIn("from-project-plugin", plugin_read.content)
            finally:
                os.chdir(old_cwd)
                if old_extra is None:
                    os.environ.pop("GATEWAY_SKILLS_DIRS", None)
                else:
                    os.environ["GATEWAY_SKILLS_DIRS"] = old_extra
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_claude_project_dir_prefers_live_primary_directory_over_stale_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_cwd = pathlib.Path.cwd()
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                service_root = pathlib.Path(td) / "gateway-service"
                project_root = pathlib.Path(td) / "PersonalAIBrain"
                service_root.mkdir()
                project_root.mkdir()
                service_skill = service_root / ".claude/skills/service-only/SKILL.md"
                service_skill.parent.mkdir(parents=True, exist_ok=True)
                service_skill.write_text("# service-only\n\nSERVICE-SHOULD-NOT-LEAK\n", encoding="utf-8")
                project_skill = project_root / ".claude/skills/project-skill/SKILL.md"
                project_skill.parent.mkdir(parents=True, exist_ok=True)
                project_skill.write_text("# project-skill\n\nPROJECT-SKILL-OK\n", encoding="utf-8")
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                os.chdir(service_root)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                text = (
                    "<system-reminder>\n"
                    f"Previous summary says stale **Worktree:** {service_root}\n"
                    "</system-reminder>\n"
                    "# Environment\n"
                    "You have been invoked in the following environment:\n"
                    f" - Primary working directory: {project_root}\n"
                    " - Is a git repository: true\n\n"
                    "Read skill project-skill and return its content."
                )
                client = FakeClient([])
                result = run_tool_orchestration(
                    "/v1/messages",
                    {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}], "max_tokens": 128},
                    client,
                )
                serialized = json.dumps(result, ensure_ascii=False)
                self.assertIn('"name": "Skill"', serialized)
                self.assertIn("project-skill", serialized)
                self.assertNotIn("SERVICE-SHOULD-NOT-LEAK", serialized)
                self.assertEqual(result.get("gateway_context", {}).get("strategy"), "gateway_downstream_tool_request")
                self.assertEqual(client.requests, [])
            finally:
                gateway.CONFIG_PATH = old_config
                os.chdir(old_cwd)
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_codex_responses_project_dir_uses_environment_context_cwd_for_skills(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                service_root = pathlib.Path(td) / "gateway-service"
                project_root = pathlib.Path(td) / "codex-project"
                service_root.mkdir()
                project_root.mkdir()
                skill_file = project_root / ".codex/skills/codex-project-skill/SKILL.md"
                skill_file.parent.mkdir(parents=True, exist_ok=True)
                skill_file.write_text("# codex-project-skill\n\nCODEX-PROJECT-SKILL-OK\n", encoding="utf-8")
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                body = {
                    "model": "m",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"<environment_context>\n  <cwd>{project_root}</cwd>\n  <shell>bash</shell>\n</environment_context>",
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Read skill codex-project-skill and return its content."}],
                        },
                    ],
                    "max_output_tokens": 128,
                }
                client = FakeClient([])
                result = run_tool_orchestration("/v1/responses", body, client)
                serialized = json.dumps(result, ensure_ascii=False)
                self.assertIn('"name": "Skill"', serialized)
                self.assertIn("codex-project-skill", serialized)
                self.assertEqual(result.get("gateway_context", {}).get("strategy"), "gateway_downstream_tool_request")
                self.assertEqual(client.requests, [])
            finally:
                gateway.CONFIG_PATH = old_config
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_project_trace_paths_use_downstream_project_root_not_gateway_service_root(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_cwd = pathlib.Path.cwd()
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                service_root = pathlib.Path(td) / "gateway-service"
                project_root = pathlib.Path(td) / "PersonalAIBrain"
                service_root.mkdir()
                project_root.mkdir()
                service_trace = service_root / ".traces/2026-05-24/trace.txt"
                service_trace.parent.mkdir(parents=True, exist_ok=True)
                service_trace.write_text("project-trace-marker: WRONG-SERVICE\n", encoding="utf-8")
                project_trace = project_root / ".traces/2026-05-24/trace.txt"
                project_trace.parent.mkdir(parents=True, exist_ok=True)
                project_trace.write_text("project-trace-marker: PERSONAL-BRAIN-TRACE-OK\n", encoding="utf-8")
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                os.chdir(service_root)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                text = (
                    "<system-reminder>\n"
                    f"Previous summary has stale **Worktree:** {service_root}\n"
                    "</system-reminder>\n"
                    "# Environment\n"
                    "You have been invoked in the following environment:\n"
                    f" - Primary working directory: {project_root}\n"
                    " - Is a git repository: true\n\n"
                    f"Read local file {project_trace} and answer only the value after project-trace-marker."
                )
                client = FakeClient([])
                result = run_tool_orchestration(
                    "/v1/messages",
                    {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}], "max_tokens": 64},
                    client,
                )
                serialized = json.dumps(result, ensure_ascii=False)
                self.assertIn(str(project_trace), serialized)
                self.assertNotIn("WRONG-SERVICE", serialized)
                tool_use = [block for block in (result.get("content") or []) if block.get("type") == "tool_use"]
                self.assertTrue(tool_use)
                self.assertEqual(tool_use[0].get("name"), "Read")
                self.assertEqual(tool_use[0].get("input", {}).get("path"), str(project_trace))
                self.assertEqual(result.get("gateway_context", {}).get("strategy"), "gateway_downstream_tool_request")
                self.assertEqual(client.requests, [])
            finally:
                gateway.CONFIG_PATH = old_config
                os.chdir(old_cwd)
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_workspace_scope_is_thread_local_for_parallel_downstream_projects(self):
        from src.gateway_builtin_tools import _workspace_root
        from src.gateway_tool_runtime import _workspace_scope

        with tempfile.TemporaryDirectory() as td:
            project_a = pathlib.Path(td) / "project-a"
            project_b = pathlib.Path(td) / "project-b"
            project_a.mkdir()
            project_b.mkdir()
            barrier = threading.Barrier(2)
            results: dict[str, str] = {}

            def worker(name, root):
                with _workspace_scope(root):
                    barrier.wait(timeout=5)
                    results[name] = str(_workspace_root())

            t1 = threading.Thread(target=worker, args=("a", project_a))
            t2 = threading.Thread(target=worker, args=("b", project_b))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)
            self.assertEqual(results, {"a": str(project_a.resolve()), "b": str(project_b.resolve())})

    def test_parallel_tool_use_inherits_active_downstream_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                service_root = pathlib.Path(td) / "gateway-service"
                project_root = pathlib.Path(td) / "parallel-project"
                service_root.mkdir()
                project_root.mkdir()
                (project_root / "target.txt").write_text("PARALLEL-PROJECT-OK\n", encoding="utf-8")
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                gateway.save_config(cfg)

                result = execute_direct_tool_call(
                    {
                        "workspace_root": str(project_root),
                        "tool": "multi_tool_use.parallel",
                        "arguments": {
                            "tool_uses": [
                                {"recipient_name": "Read", "parameters": {"file_path": "target.txt"}},
                                {"recipient_name": "Tree", "parameters": {"path": ".", "max_depth": 1}},
                            ]
                        },
                    }
                )
                serialized = json.dumps(result, ensure_ascii=False)
                self.assertTrue(result["success"])
                self.assertIn("PARALLEL-PROJECT-OK", serialized)
                self.assertIn("target.txt", serialized)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_memory_tool_lists_only_active_downstream_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "gateway.sqlite3")
            gateway.SQLITE_READY = False
            try:
                service_root = pathlib.Path(td) / "gateway-service"
                project_root = pathlib.Path(td) / "PersonalAIBrain"
                service_root.mkdir()
                project_root.mkdir()
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                gateway.save_config(cfg)

                service_memory = execute_direct_tool_call(
                    {
                        "workspace_root": str(service_root),
                        "tool": "SaveMemory",
                        "arguments": {"action": "write", "summary": "SERVICE-MEMORY-WRONG", "keywords": ["service-memory"]},
                    }
                )
                self.assertTrue(service_memory["success"])
                project_memory = execute_direct_tool_call(
                    {
                        "workspace_root": str(project_root),
                        "tool": "SaveMemory",
                        "arguments": {"action": "write", "summary": "PROJECT-MEMORY-OK", "keywords": ["project-memory"]},
                    }
                )
                self.assertTrue(project_memory["success"])
                self.assertIn(str(project_root), project_memory["content"])

                recalled = execute_direct_tool_call(
                    {"workspace_root": str(project_root), "tool": "RecallMemory", "arguments": {"action": "list", "limit": 10}}
                )
                self.assertTrue(recalled["success"])
                self.assertIn("PROJECT-MEMORY-OK", recalled["content"])
                self.assertIn(str(project_root), recalled["content"])
                self.assertNotIn("SERVICE-MEMORY-WRONG", recalled["content"])
                self.assertNotIn(str(service_root), recalled["content"])
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.SQLITE_READY = old_ready
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_weak_upstream_direct_skill_requests_delegate_to_downstream(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                workspace = pathlib.Path(td) / "workspace"
                skill_file = workspace / ".claude/skills/reason-boost/SKILL.md"
                skill_file.parent.mkdir(parents=True, exist_ok=True)
                skill_file.write_text("# reason-boost\n\nUse first principles. marker: SKILL-BOOST-OK\n", encoding="utf-8")
                os.environ["GATEWAY_WORKSPACE_ROOT"] = str(workspace)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                messages_client = FakeClient([])
                messages_result = run_tool_orchestration(
                    "/v1/messages",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "Read skill reason-boost and return its content."}],
                        "max_tokens": 128,
                    },
                    messages_client,
                )
                skill_use = [block for block in (messages_result.get("content") or []) if block.get("type") == "tool_use"]
                self.assertTrue(skill_use)
                self.assertEqual(skill_use[0].get("name"), "Skill")
                self.assertEqual(skill_use[0].get("input", {}).get("name"), "reason-boost")
                self.assertEqual(messages_result.get("gateway_context", {}).get("strategy"), "gateway_downstream_tool_request")
                self.assertEqual(messages_client.requests, [])

                responses_client = FakeClient([])
                responses_result = run_tool_orchestration(
                    "/v1/responses",
                    {
                        "model": "m",
                        "input": "List skills available in this workspace.",
                        "max_output_tokens": 256,
                    },
                    responses_client,
                )
                serialized = json.dumps(responses_result, ensure_ascii=False)
                self.assertIn('"name": "Skill"', serialized)
                self.assertEqual(responses_result.get("gateway_context", {}).get("strategy"), "gateway_downstream_tool_request")
                self.assertEqual(responses_client.requests, [])
            finally:
                gateway.CONFIG_PATH = old_config
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_core_coding_tools_write_edit_shell_and_web_are_real(self):
        class SearchHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: N802
                pass

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/search"):
                    body = (
                        '<html><body><a class="result__a" href="https://example.test/result">Example Result</a>'
                        '<a class="result__snippet">Snippet text</a></body></html>'
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("content-type", "text/html")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = b"hello network"
                self.send_response(200)
                self.send_header("content-type", "text/plain")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                self.do_GET()

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_force = os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = "0"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), SearchHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["allow_write_tools"] = True
                cfg["gateway"]["allow_shell_tools"] = True
                gateway.save_config(cfg)
                wrote = _execute_tool_call(ToolCall("w", "Write", {"file_path": "smoke/app.py", "content": "print('alpha')\n"}, {}))
                self.assertTrue(wrote.success)
                edited = _execute_tool_call(ToolCall("e", "Edit", {"file_path": "smoke/app.py", "old_string": "alpha", "new_string": "beta"}, {}))
                self.assertTrue(edited.success)
                read = _execute_tool_call(ToolCall("r", "Read", {"file_path": "smoke/app.py"}, {}))
                self.assertIn("beta", read.content)
                shell = _execute_tool_call(ToolCall("b", "Bash", {"command": "python3 smoke/app.py", "timeout": 5}, {}))
                self.assertTrue(shell.success)
                self.assertIn("exit_code=0", shell.content)
                self.assertIn("beta", shell.content)
                fetched = _execute_tool_call(ToolCall("f", "WebFetch", {"url": f"http://127.0.0.1:{httpd.server_address[1]}/page"}, {}))
                self.assertTrue(fetched.success)
                self.assertIn("hello network", fetched.content)
                searched = _execute_tool_call(
                    ToolCall("s", "WebSearch", {"query": "example", "search_url": f"http://127.0.0.1:{httpd.server_address[1]}/search"}, {})
                )
                self.assertTrue(searched.success)
                self.assertIn("Example Result", searched.content)
                posted = _execute_tool_call(
                    ToolCall("post", "WebFetch", {"url": f"http://127.0.0.1:{httpd.server_address[1]}/page", "method": "POST", "json": {"hello": "world"}}, {})
                )
                self.assertTrue(posted.success)
                self.assertIn("status: 200", posted.content)
                fenced = _execute_tool_call(ToolCall("ci", "code_interpreter", {"description": "```python\nprint(21*2)\n```", "timeout": 5}, {}))
                self.assertTrue(fenced.success)
                self.assertIn("42", fenced.content)
                png = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
                )
                pathlib.Path(td, "red.png").write_bytes(png)
                image = _execute_tool_call(ToolCall("img", "AnalyzeImage", {"path": "red.png", "histogram": True}, {}))
                self.assertTrue(image.success)
                image_payload = json.loads(image.content)
                self.assertEqual(image_payload["width"], 1)
                self.assertEqual(image_payload["height"], 1)
                intent = _execute_tool_call(ToolCall("intent", "IntentDetect", {"text": "分析 @src/gateway_app.py 并修改代码，然后运行测试和查询网络"}, {}))
                self.assertTrue(intent.success)
                intent_payload = json.loads(intent.content)
                self.assertIn("project_analysis", intent_payload["intents"])
                self.assertIn("code_change", intent_payload["intents"])
                self.assertIn("network", intent_payload["intents"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_image_generation_does_not_fake_success_when_providers_fail(self):
        import urllib.request as _urllib_request
        from src import gateway_computer_use as cu

        old_urlopen = _urllib_request.urlopen
        old_openai = os.environ.get("OPENAI_API_KEY")
        old_image_key = os.environ.get("IMAGE_GEN_API_KEY")
        old_hf = os.environ.get("HF_TOKEN")
        old_hf2 = os.environ.get("HUGGINGFACE_TOKEN")
        old_pil = cu._PIL_Image

        class FakeImage:
            width = 512
            height = 512

            def save(self, target, format=None):
                if hasattr(target, "write"):
                    target.write(b"fake-png")
                else:
                    pathlib.Path(target).write_bytes(b"fake-png")

        class FakePIL:
            @staticmethod
            def new(*args, **kwargs):
                return FakeImage()

        try:
            _urllib_request.urlopen = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network unavailable"))
            cu._PIL_Image = FakePIL
            for key in ("OPENAI_API_KEY", "IMAGE_GEN_API_KEY", "HF_TOKEN", "HUGGINGFACE_TOKEN"):
                os.environ.pop(key, None)
            payload = json.loads(cu._tool_image_generation({"prompt": "draw a gateway"}))
            self.assertFalse(payload["ok"])
            self.assertNotEqual(payload.get("provider"), "local_placeholder")
            self.assertIn("No real image generation provider", payload["error"])

            result = _execute_tool_call(ToolCall("imggen", "image_generation", {"prompt": "draw a gateway"}, {}))
            self.assertFalse(result.success)
            self.assertEqual(result.failure_type, "connector_required")
            self.assertIn("No real image generation provider", result.content)
        finally:
            _urllib_request.urlopen = old_urlopen
            cu._PIL_Image = old_pil
            for key, value in {
                "OPENAI_API_KEY": old_openai,
                "IMAGE_GEN_API_KEY": old_image_key,
                "HF_TOKEN": old_hf,
                "HUGGINGFACE_TOKEN": old_hf2,
            }.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

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
        old_protocol = os.environ.get("UPSTREAM_PROTOCOL")
        os.environ["UPSTREAM_PROTOCOL"] = "openai_chat"
        try:
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
        finally:
            if old_protocol:
                os.environ["UPSTREAM_PROTOCOL"] = old_protocol
            else:
                os.environ.pop("UPSTREAM_PROTOCOL", None)

    def test_gateway_config_max_tool_rounds_limits_orchestration_loop(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_env = os.environ.get("GATEWAY_MAX_TOOL_ROUNDS")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                os.environ.pop("GATEWAY_MAX_TOOL_ROUNDS", None)
                cfg = gateway._default_config()
                cfg["gateway"]["max_tool_rounds"] = 1
                gateway.save_config(cfg)
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
                        }
                    ]
                )
                with self.assertRaises(gateway.GatewayError) as cm:
                    run_tool_orchestration(
                        "/v1/chat/completions",
                        {"model": "m", "messages": [{"role": "user", "content": "calc"}]},
                        client,
                    )
                self.assertEqual(str(cm.exception), "max tool rounds exceeded")
                self.assertEqual(cm.exception.detail["max_tool_rounds"], 1)
                self.assertEqual(len(client.requests), 1)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_env is None:
                    os.environ.pop("GATEWAY_MAX_TOOL_ROUNDS", None)
                else:
                    os.environ["GATEWAY_MAX_TOOL_ROUNDS"] = old_env

    def test_configured_max_tool_rounds_resolves_env_over_config_over_default(self):
        from src.gateway_config import _configured_max_tool_rounds
        from unittest.mock import patch

        old_env = os.environ.get("GATEWAY_MAX_TOOL_ROUNDS")
        try:
            # env var takes highest priority
            os.environ["GATEWAY_MAX_TOOL_ROUNDS"] = "3"
            self.assertEqual(_configured_max_tool_rounds({"max_tool_rounds": 99}), 3)

            # config value used when env var absent
            os.environ.pop("GATEWAY_MAX_TOOL_ROUNDS", None)
            self.assertEqual(_configured_max_tool_rounds({"max_tool_rounds": 7}), 7)

            # default 5 when neither env nor config (explicit empty dict)
            self.assertEqual(_configured_max_tool_rounds({}), 5)

            # default 5 when None passed and config has no max_tool_rounds
            with patch("src.gateway_config._gateway_config", return_value={}):
                self.assertEqual(_configured_max_tool_rounds(None), 5)

            # invalid env var falls back to default
            os.environ["GATEWAY_MAX_TOOL_ROUNDS"] = "not-a-number"
            self.assertEqual(_configured_max_tool_rounds({}), 5)

            # invalid config value falls back to default
            os.environ.pop("GATEWAY_MAX_TOOL_ROUNDS", None)
            self.assertEqual(_configured_max_tool_rounds({"max_tool_rounds": "bad"}), 5)
        finally:
            if old_env is None:
                os.environ.pop("GATEWAY_MAX_TOOL_ROUNDS", None)
            else:
                os.environ["GATEWAY_MAX_TOOL_ROUNDS"] = old_env

    def test_admin_form_numeric_raw_rejects_empty_keys(self):
        from src.gateway_config import _admin_form_numeric_raw

        with self.assertRaises(ValueError):
            _admin_form_numeric_raw({}, (), None, 0)

    def test_resolved_text_tool_adapter_compact_token_limit_dynamic(self):
        from src.gateway_config import (
            _TEXT_TOOL_ADAPTER_COMPACT_DEFAULT_CAP,
            _TEXT_TOOL_ADAPTER_COMPACT_FLOOR,
            _TEXT_TOOL_ADAPTER_COMPACT_RATIO,
            _resolved_text_tool_adapter_compact_token_limit,
        )

        old_env = os.environ.get("GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT")
        try:
            os.environ.pop("GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT", None)

            # small upstream: hits floor
            small = {"max_input_tokens": 4000}
            result = _resolved_text_tool_adapter_compact_token_limit({}, small)
            self.assertEqual(result, _TEXT_TOOL_ADAPTER_COMPACT_FLOOR)

            # medium upstream: dynamic = 32000 * 0.45 = 14400, below cap
            medium = {"max_input_tokens": 32000}
            result = _resolved_text_tool_adapter_compact_token_limit({}, medium)
            self.assertEqual(result, int(32000 * _TEXT_TOOL_ADAPTER_COMPACT_RATIO))

            # large upstream: dynamic = 128000 * 0.45 = 57600, capped at 48000
            large = {"max_input_tokens": 128000}
            result = _resolved_text_tool_adapter_compact_token_limit({}, large)
            self.assertEqual(result, _TEXT_TOOL_ADAPTER_COMPACT_DEFAULT_CAP)

            # 1M upstream: dynamic huge, capped at 48000
            huge = {"max_input_tokens": 1000000}
            result = _resolved_text_tool_adapter_compact_token_limit({}, huge)
            self.assertEqual(result, _TEXT_TOOL_ADAPTER_COMPACT_DEFAULT_CAP)

            # custom config cap overrides default
            result = _resolved_text_tool_adapter_compact_token_limit(
                {"text_tool_adapter_compact_token_limit": 20000}, large
            )
            self.assertEqual(result, 20000)

            # env var overrides config cap; dynamic = 128000 * 0.45 = 57600 < 100000
            os.environ["GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT"] = "100000"
            result = _resolved_text_tool_adapter_compact_token_limit({}, large)
            self.assertEqual(result, int(128000 * _TEXT_TOOL_ADAPTER_COMPACT_RATIO))

            # env var cap = 200000 with 1M upstream: dynamic = 450000, capped at 200000
            os.environ["GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT"] = "200000"
            result = _resolved_text_tool_adapter_compact_token_limit({}, huge)
            self.assertEqual(result, 200000)

            # config cap = 0 disables compaction
            os.environ.pop("GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT", None)
            result = _resolved_text_tool_adapter_compact_token_limit(
                {"text_tool_adapter_compact_token_limit": 0}, large
            )
            self.assertEqual(result, 0)

            # config cap = 0 via env var also disables
            os.environ["GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT"] = "0"
            result = _resolved_text_tool_adapter_compact_token_limit({}, large)
            self.assertEqual(result, 0)

            # missing upstream config defaults to 128000; dynamic = 57600, capped at default 48000
            os.environ.pop("GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT", None)
            result = _resolved_text_tool_adapter_compact_token_limit({}, {})
            self.assertEqual(result, _TEXT_TOOL_ADAPTER_COMPACT_DEFAULT_CAP)
        finally:
            if old_env is None:
                os.environ.pop("GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT", None)
            else:
                os.environ["GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT"] = old_env

    def test_inline_bash_function_markup_repairs_missing_spaces(self):
        text = """好的，我来系统地做这件事。
  <function=Bash>find /Users/sanbo/Desktop/ai_tool_functioncall -type f-name "*.py" | head-30
  <parameter=description>List allPython files in theproject

  <function=Bash>ls -la /Users/sanbo/Desktop/ai_tool_functioncall/scripts/2>/dev/null; ls
  -la/Users/sanbo/Desktop/ai_tool_functioncall/src/ 2>/dev/null
  <parameter=description>List scriptsand src directories"""
        calls = _parse_text_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].name, "Bash")
        self.assertIn('-type f -name "*.py"', calls[0].arguments["command"])
        self.assertIn("ls -la /Users", calls[1].arguments["command"])

    def test_parameter_only_bash_markup_repairs_missing_spaces(self):
        text = """<parameter=command>find/Users/sanbo/Desktop/ai_tool_functioncall/src
  -typef -name '.py' | head-30
  <parameter=description>List allPython source files insrc/
  <parameter=command>find /Users/sanbo/Desktop/ai_tool_functioncall/tests -typef -name '.py' | head-30
  <parameter=description>List allPython test files intests/
  <parameter=command>find /Users/sanbo/Desktop/ai_tool_functioncall/src -name'*.py' -exec wc -l{} + 2>/dev/null |sort -n| tail
  -20<parameter=description>Count lines ofcode in src/"""
        calls = _parse_text_tool_calls(text)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call.name == "Bash" for call in calls))
        self.assertIn("find /Users", calls[0].arguments["command"])
        self.assertIn("-type f", calls[0].arguments["command"])
        self.assertIn("head -30", calls[0].arguments["command"])
        self.assertIn("-type f", calls[1].arguments["command"])
        self.assertIn("wc -l {}", calls[2].arguments["command"])

    def test_text_read_markup_trims_noisy_path_parameters(self):
        text = """<function=Read>
<parameter=file_path>README.md
<tool_call>

<function=Read>src/gateway_app.py

---## 基于真实文件的完整审查报告"""
        calls = [gateway._normalize_tool_call(call) for call in _parse_text_tool_calls(text)]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].name, "Read")
        # After normalization, file_path is mapped to path
        self.assertEqual(calls[0].arguments["path"], "README.md")
        self.assertEqual(calls[1].arguments["path"], "src/gateway_app.py")

    def test_text_function_markup_fallback_executes_local_tools(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_force = os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = "0"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["text_tool_call_fallback_enabled"] = True
                cfg["gateway"]["delegate_tools_to_downstream"] = False
                gateway.save_config(cfg)
                pathlib.Path(td, "a.py").write_text("print('a')\n", encoding="utf-8")
                pathlib.Path(td, "README.md").write_text("# demo\n", encoding="utf-8")
                client = FakeClient(
                    [
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "Let me explore.\n<function=Glob>\n<parameter=pattern>/*.py\n\n<function=Glob>\n<parameter=pattern>/*.md",
                                    }
                                }
                            ]
                        },
                        {"choices": [{"message": {"role": "assistant", "content": "found a.py and README.md"}}]},
                    ]
                )
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "m", "messages": [{"role": "user", "content": "分析这套代码"}]},
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "found a.py and README.md")
                self.assertEqual(len(client.requests), 2)
                report = client.requests[1][1]["messages"][-1]["content"]
                self.assertIn("gateway_local_tool_fallback", report)
                self.assertIn("a.py", report)
                self.assertIn("README.md", report)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_no_native_tools_upstream_strips_schemas_but_executes_local_text_tool_calls(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["tools_enabled"] = "auto"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                cfg["gateway"]["text_tool_call_fallback_enabled"] = True
                cfg["gateway"]["delegate_tools_to_downstream"] = False
                gateway.save_config(cfg)
                pathlib.Path(td, "x.py").write_text("print('x')\n", encoding="utf-8")
                client = FakeClient(
                    [
                        {"choices": [{"message": {"role": "assistant", "content": "<function=Glob>\n<parameter=pattern>/*.py"}}]},
                        {"choices": [{"message": {"role": "assistant", "content": "saw x.py"}}]},
                    ]
                )
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "分析这套代码"}],
                        "tools": [{"type": "function", "function": {"name": "FakeTool"}}],
                        "tool_choice": "auto",
                    },
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "saw x.py")
                first_request = client.requests[0][1]
                self.assertNotIn("tools", first_request)
                self.assertNotIn("tool_choice", first_request)
                self.assertIn("Tool Call Gateway", first_request["messages"][0]["content"])
                self.assertIn("x.py", client.requests[1][1]["messages"][-1]["content"])
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_text_tool_adapter_compacts_huge_claude_code_payload_before_upstream(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["tools_enabled"] = "auto"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                cfg["gateway"]["text_tool_adapter_compact_token_limit"] = 1000
                cfg["context"]["enabled"] = True
                cfg["context"]["summary_max_chars"] = 1000
                gateway.save_config(cfg)

                huge_tool_schema = [
                    {
                        "name": "Bash",
                        "description": "run shell " + ("x" * 12000),
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "description": "command " + ("y" * 12000)}
                            },
                            "required": ["command"],
                        },
                    }
                ]
                huge_reminder = "<system-reminder>\n" + ("skill list\n" * 3000) + "</system-reminder>"
                client = FakeClient(
                    [
                        {
                            "id": "chatcmpl_compact",
                            "object": "chat.completion",
                            "model": "m",
                            "choices": [
                                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                            ],
                        }
                    ]
                )
                final = run_tool_orchestration(
                    "/v1/messages",
                    {
                        "model": "m",
                        "system": [{"type": "text", "text": "system " + ("z" * 20000)}],
                        "tools": huge_tool_schema,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": huge_reminder},
                                    {"type": "text", "text": "Reply with OK only."},
                                ],
                            }
                        ],
                        "max_tokens": 32,
                    },
                    client,
                )
                self.assertEqual(final["content"][0]["text"], "ok")
                sent = client.requests[0][1]
                serialized = json.dumps(sent, ensure_ascii=False)
                self.assertLess(len(serialized), 30000)
                self.assertLess(gateway._body_token_estimate(sent), 10000)
                self.assertNotIn("x" * 2000, serialized)
                self.assertNotIn("skill list\n" * 200, serialized)
                self.assertIn("Tool Call Gateway", serialized)
                self.assertIn("gateway context compacted", serialized)
                self.assertNotIn("tools", sent)
                self.assertNotIn("tool_choice", sent)
            finally:
                gateway.CONFIG_PATH = old_config

    def test_over_limit_claude_code_request_is_compacted_before_upstream(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["context"]["enabled"] = True
                cfg["context"]["max_input_tokens"] = 100
                cfg["context"]["summary_max_chars"] = 2000
                gateway.save_config(cfg)
                huge_tool_schema = [{"name": "HugeTool", "description": "x" * 20000, "input_schema": {"type": "object"}}]
                huge_system = "system " + ("y" * 20000)
                client = FakeClient([{"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}])
                final = run_tool_orchestration(
                    "/v1/messages",
                    {
                        "model": "m",
                        "system": huge_system,
                        "tools": huge_tool_schema,
                        "messages": [{"role": "user", "content": [{"type": "text", "text": "分析 @src/ 中所有代码，逐个类分析"}]}],
                        "max_tokens": 100,
                    },
                    client,
                )
                self.assertEqual(final["content"][0]["text"], "ok")
                sent = client.requests[0][1]
                self.assertLess(gateway._body_token_estimate(sent), 24000)
                self.assertNotIn("HugeTool", json.dumps(sent, ensure_ascii=False))
                self.assertIn("tool_use", json.dumps(sent, ensure_ascii=False))
                self.assertIn("分析 @src/", json.dumps(sent, ensure_ascii=False))
            finally:
                gateway.CONFIG_PATH = old_config

    def test_local_planner_surfaces_absolute_file_path_to_downstream(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                workspace = pathlib.Path(td) / "workspace"
                workspace.mkdir()
                probe = workspace / "probe.txt"
                probe.write_text("gateway-local-file-probe: 2+2=4\n", encoding="utf-8")
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["gateway"]["local_planner_enabled"] = True
                cfg["upstream"]["tools_enabled"] = "auto"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)
                client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "2+2=4"}}]}])
                final = run_tool_orchestration(
                    "/v1/messages",
                    {
                        "model": "m",
                        "messages": [
                            {
                                "role": "user",
                                "content": f"Read local file {probe} and answer only the value after gateway-local-file-probe.",
                            }
                        ],
                        "max_tokens": 64,
                    },
                    client,
                )
                tool_use = [block for block in (final.get("content") or []) if block.get("type") == "tool_use"]
                self.assertTrue(tool_use, "local file reads must be delegated to the downstream client")
                self.assertEqual(tool_use[0].get("name"), "Read")
                self.assertEqual(tool_use[0].get("input", {}).get("path"), str(probe))
                self.assertEqual(final.get("stop_reason"), "tool_use")
                self.assertEqual(final.get("gateway_context", {}).get("strategy"), "gateway_downstream_tool_request")
                self.assertEqual(client.requests, [])
            finally:
                gateway.CONFIG_PATH = old_config

    def test_user_side_bash_text_tool_is_delegated_not_executed_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)
                client = FakeClient([
                    {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "<function=Bash>\n<parameter=command>pwd</parameter>\n</function>",
                            },
                            "finish_reason": "stop",
                        }]
                    }
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "Please use a shell diagnostic if needed."}],
                        "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}],
                    },
                    client,
                )
                choice = final["choices"][0]
                self.assertEqual(choice.get("finish_reason"), "tool_calls")
                tool_calls = choice["message"].get("tool_calls") or []
                self.assertEqual(tool_calls[0]["function"]["name"], "Bash")
                self.assertIn("pwd", tool_calls[0]["function"]["arguments"])
                self.assertEqual(len(client.requests), 1)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_current_directory_request_surfaces_client_ls_without_gateway_repo(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)
                client = FakeClient([])
                final = run_tool_orchestration(
                    "/v1/messages",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "请查看当前目录下有哪些文件"}],
                        "max_tokens": 128,
                    },
                    client,
                )
                serialized = json.dumps(final, ensure_ascii=False)
                self.assertIn('"name": "LS"', serialized)
                self.assertIn('"path": "."', serialized)
                self.assertNotIn("CLAUDE.md", serialized)
                self.assertEqual(final.get("stop_reason"), "tool_use")
                self.assertEqual(client.requests, [])
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_gateway_owned_weather_http_action_executes_and_roundtrips(self):
        class WeatherHandler(BaseHTTPRequestHandler):
            seen = []

            def log_message(self, fmt, *args):
                return

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                WeatherHandler.seen.append(urllib.parse.parse_qs(parsed.query))
                payload = b'{"temp_c":21,"condition":"sunny"}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), WeatherHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [
                        {
                            "name": "get_weather",
                            "description": "Get current weather",
                            "method": "GET",
                            "url": f"http://127.0.0.1:{httpd.server_address[1]}/weather",
                            "input_schema": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                            },
                        }
                    ],
                }
                gateway.save_config(cfg)
                client = FakeClient([
                    {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "<function=get_weather>\n<parameter=city>Shanghai</parameter>\n</function>",
                            },
                            "finish_reason": "stop",
                        }]
                    },
                    {"choices": [{"message": {"role": "assistant", "content": "Shanghai weather is sunny, 21C."}, "finish_reason": "stop"}]},
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "Weather in Shanghai?"}],
                        "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
                    },
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "Shanghai weather is sunny, 21C.")
                self.assertEqual(WeatherHandler.seen[0]["city"], ["Shanghai"])
                self.assertIn("temp_c", client.requests[1][1]["messages"][-1]["content"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_context_fanout_splits_large_chat_then_synthesizes(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["context"]["enabled"] = True
                cfg["context"]["fanout_enabled"] = True
                cfg["context"]["max_input_tokens"] = 10
                cfg["context"]["fanout_chunk_tokens"] = 250
                cfg["context"]["fanout_max_chunks"] = 2
                gateway.save_config(cfg)
                client = FakeClient(
                    [
                        {"choices": [{"message": {"role": "assistant", "content": "partial A"}}]},
                        {"choices": [{"message": {"role": "assistant", "content": "partial B"}}]},
                        {"choices": [{"message": {"role": "assistant", "content": "final synthesis"}}]},
                        {"choices": [{"message": {"role": "assistant", "content": "checked final synthesis"}}]},
                    ]
                )
                large_prompt = "分析这些类\n" + ("class A {}\n" * 300) + "\n\n" + ("class B {}\n" * 300)
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "m", "messages": [{"role": "user", "content": large_prompt}]},
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "checked final synthesis")
                self.assertEqual(final["gateway_context"]["strategy"], "fanout_synthesis")
                self.assertEqual(final["gateway_context"]["chunks"], 2)
                self.assertTrue(final["gateway_context"]["quality_reviewed"])
                self.assertEqual(len(client.requests), 4)
                self.assertIn("片段 1/2", client.requests[0][1]["messages"][-1]["content"])
                self.assertIn("片段 2/2", client.requests[1][1]["messages"][-1]["content"])
                self.assertIn("子分析 1", client.requests[2][1]["messages"][-1]["content"])
                self.assertIn("质量审查器", client.requests[3][1]["messages"][-1]["content"])
                self.assertNotIn("tools", client.requests[0][1])
            finally:
                gateway.CONFIG_PATH = old_config

    def test_default_config_and_admin_post_save_upstream_capabilities(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                self.assertIn("capabilities", cfg["upstream"])
                self.assertFalse(cfg["upstream"]["capabilities"]["supports_tools"])
                self.assertEqual(cfg["context"]["fanout_max_chunks"], 0)
                gateway.save_config(cfg)
                token = base64.b64encode(b"admin:admin").decode("ascii")
                form = {
                    "base_url": "http://upstream.local",
                    "model": "mimo-v2.5-pro",
                    "protocol": "anthropic_messages",
                    "tools_enabled": "auto",
                    "native_tools_verified": "1",
                    "use_for_coding": "1",
                    "upstream_timeout_seconds": "45",
                    "upstream_max_input_tokens": "200000",
                    "upstream_max_output_tokens": "16000",
                    "upstream_max_concurrency": "64",
                    "cap_supports_streaming": "1",
                    "cap_supports_tools": "1",
                    "cap_supports_function_calls": "1",
                    "cap_supports_parallel_tool_calls": "1",
                    "cap_supports_vision": "1",
                    "cap_supports_network": "1",
                    "cap_supports_web_search": "1",
                    "cap_supports_json_schema": "1",
                    "path_models": "/openai/models",
                    "path_chat_completions": "/openai/chat",
                    "path_responses": "/openai/responses",
                    "path_messages": "/anthropic/messages",
                    "tool_mode": "orchestrate",
                    "max_tool_rounds": "7",
                    "max_concurrent_requests": "48",
                    "concurrency_queue_timeout_seconds": "3",
                    "tool_execution_timeout_seconds": "90",
                    "text_tool_adapter_compact_token_limit": "10000",
                    "workspace_root": td,
                    "allow_write_tools": "1",
                    "allow_shell_tools": "1",
                    "request_logging": "1",
                    "record_unsupported_tools": "1",
                    "text_tool_call_fallback_enabled": "1",
                    "context_enabled": "1",
                    "context_fanout_enabled": "1",
                    "context_quality_review_enabled": "1",
                    "context_max_input_tokens": "8000",
                    "context_fanout_chunk_tokens": "6000",
                    "context_fanout_max_chunks": "0",
                    "context_fanout_max_workers": "6",
                }
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/config",
                    data=urllib.parse.urlencode(form).encode("utf-8"),
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=5).read()
                except Exception as exc:
                    # urllib may surface the post-save redirect depending on opener policy; config persistence is the assertion.
                    if not getattr(exc, "code", None) == 303:
                        raise
                saved = gateway.load_config()
                self.assertTrue(saved["upstream"]["capabilities"]["supports_vision"])
                self.assertTrue(saved["upstream"]["capabilities"]["supports_network"])
                self.assertTrue(saved["upstream"]["capabilities"]["supports_web_search"])
                self.assertEqual(saved["active_upstream"], "default")
                self.assertEqual(saved["upstream_profiles"][0]["id"], "default")
                self.assertEqual(saved["upstream"]["paths"]["messages"], "/anthropic/messages")
                self.assertEqual(saved["upstream"]["timeout_seconds"], 45.0)
                self.assertEqual(saved["gateway"]["max_concurrent_requests"], 48)
                self.assertEqual(saved["gateway"]["text_tool_adapter_compact_token_limit"], 10000)
                self.assertTrue(saved["gateway"]["text_tool_call_fallback_enabled"])
                self.assertEqual(saved["context"]["fanout_max_chunks"], 0)
                self.assertEqual(saved["context"]["fanout_max_workers"], 6)
                self.assertTrue(saved["context"]["quality_review_enabled"])
                self.assertEqual(gateway._configured_upstream_path("/v1/messages"), "/anthropic/messages")
                add_form = dict(form)
                add_form.update({
                    "profile_id": "chat-only",
                    "profile_name": "Chat Only Upstream",
                    "base_url": "http://chat-only.local",
                    "model": "chat-only-model",
                    "protocol": "openai_chat",
                    "cap_supports_tools": "",
                    "cap_supports_function_calls": "",
                    "path_chat_completions": "/v1/chat/completions",
                    "path_responses": "/not-supported",
                    "path_messages": "/not-supported",
                })
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/config",
                    data=urllib.parse.urlencode(add_form).encode("utf-8"),
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=5).read()
                except Exception as exc:
                    if not getattr(exc, "code", None) == 303:
                        raise
                saved = gateway.load_config()
                self.assertEqual(saved["active_upstream"], "chat-only")
                self.assertEqual(saved["upstream"]["base_url"], "http://chat-only.local")
                self.assertEqual(len(saved["upstream_profiles"]), 2)
                self.assertFalse(saved["upstream"]["capabilities"]["supports_tools"])
                page = urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_address[1]}/ui",
                        headers={"authorization": f"Basic {token}"},
                    ),
                    timeout=5,
                ).read().decode("utf-8")
                self.assertIn("Gateway Control Center", page)
                self.assertIn("上游模型列表", page)
                self.assertIn("chat-only", page)
                self.assertIn("Fetch Models", page)
                self.assertIn("能力声明", page)
                self.assertIn('name="cap_supports_tools"', page)
                self.assertIn('name="cap_supports_vision"', page)
                self.assertIn("上下文管理", page)
                self.assertIn("/admin/upstream-models.json", page)
                self.assertIn("/anthropic", page)
                self.assertIn("claude_mnative()", page)
                self.assertIn("ANTHROPIC_AUTH_TOKEN", page)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_admin_upstream_models_endpoint_fetches_active_or_form_upstream(self):
        class ModelsHandler(BaseHTTPRequestHandler):
            seen: list[dict] = []

            def do_GET(self):  # noqa: N802
                ModelsHandler.seen.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("authorization"),
                        "x_api_key": self.headers.get("x-api-key"),
                    }
                )
                payload = json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {"id": "mimo-v2.5-pro"},
                            {"id": "mimo-v2.5"},
                            {"id": "mimo-v2.5-pro"},
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):  # noqa: N802
                pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
                cfg["upstream"]["api_key"] = "up-key"
                cfg["upstream"]["model"] = "mimo-v2.5-pro"
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["paths"]["models"] = "/v1/models"
                gateway.save_config(cfg)
                token = base64.b64encode(b"admin:admin").decode("ascii")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/upstream-models.json",
                    headers={"authorization": f"Basic {token}"},
                )
                response = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
                self.assertTrue(response["ok"])
                self.assertEqual(response["active_model"], "mimo-v2.5-pro")
                self.assertEqual(response["models"], ["mimo-v2.5", "mimo-v2.5-pro"])
                self.assertEqual(ModelsHandler.seen[-1]["path"], "/v1/models")
                self.assertEqual(ModelsHandler.seen[-1]["authorization"], "Bearer up-key")

                class QueryOverrideSink(BaseHTTPRequestHandler):
                    seen: list[dict] = []

                    def do_GET(self):  # noqa: N802
                        QueryOverrideSink.seen.append({"authorization": self.headers.get("authorization")})
                        payload = json.dumps({"data": [{"id": "evil"}]}).encode("utf-8")
                        self.send_response(200)
                        self.send_header("content-type", "application/json")
                        self.send_header("content-length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)

                    def log_message(self, fmt, *args):  # noqa: N802
                        pass

                sink = ThreadingHTTPServer(("127.0.0.1", 0), QueryOverrideSink)
                sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
                sink_thread.start()
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_address[1]}/admin/upstream-models.json?"
                        f"base_url=http://127.0.0.1:{sink.server_address[1]}&path_models=/v1/models",
                        headers={"authorization": f"Basic {token}"},
                    )
                    response = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
                    self.assertEqual(response["models"], ["mimo-v2.5", "mimo-v2.5-pro"])
                    self.assertEqual(QueryOverrideSink.seen, [])
                finally:
                    sink.shutdown()
                    sink.server_close()
                    sink_thread.join(timeout=2)

                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/upstream-models.json",
                    data=urllib.parse.urlencode(
                        {
                            "base_url": f"http://127.0.0.1:{upstream.server_address[1]}",
                            "api_key": "anth-key",
                            "protocol": "anthropic_messages",
                            "path_models": "/v1/models",
                        }
                    ).encode("utf-8"),
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                response = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
                self.assertEqual(response["models"], ["mimo-v2.5", "mimo-v2.5-pro"])
                self.assertEqual(ModelsHandler.seen[-1]["x_api_key"], "anth-key")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_admin_numeric_form_errors_return_400_without_mutating_config(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["public_base_url"] = "http://before.local:8885"
                cfg["gateway"]["client_snippet_api_key"] = "before-key"
                cfg["gateway"]["max_concurrent_requests"] = 32
                cfg["context"]["max_input_tokens"] = 24000
                gateway.save_config(cfg)
                token = base64.b64encode(b"admin:admin").decode("ascii")
                base = f"http://127.0.0.1:{httpd.server_address[1]}"

                bad_config = urllib.parse.urlencode(
                    {
                        "base_url": "http://upstream.local",
                        "model": "mimo-v2.5-pro",
                        "protocol": "anthropic_messages",
                        "upstream_timeout_seconds": "45",
                        "upstream_max_input_tokens": "200000",
                        "upstream_max_output_tokens": "16000",
                        "upstream_max_concurrency": "64",
                        "max_concurrent_requests": "not-a-number",
                        "context_max_input_tokens": "8000",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    base + "/admin/config",
                    data=bad_config,
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as config_error:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(config_error.exception.code, 400)
                payload = json.loads(config_error.exception.read().decode("utf-8"))
                self.assertIn("invalid numeric field: max_concurrent_requests", payload["error"]["message"])
                saved = gateway.load_config()
                self.assertEqual(saved["gateway"]["max_concurrent_requests"], 32)
                self.assertEqual(saved["context"]["max_input_tokens"], 24000)
                self.assertEqual(saved["gateway"]["public_base_url"], "http://before.local:8885")

                bad_upstream = urllib.parse.urlencode(
                    {
                        "base_url": "http://upstream-mutated.local",
                        "model": "mimo-v2.5-pro",
                        "protocol": "anthropic_messages",
                        "upstream_timeout_seconds": "45",
                        "upstream_max_input_tokens": "bad-input-limit",
                        "upstream_max_output_tokens": "16000",
                        "upstream_max_concurrency": "64",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    base + "/admin/config",
                    data=bad_upstream,
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as upstream_error:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(upstream_error.exception.code, 400)
                payload = json.loads(upstream_error.exception.read().decode("utf-8"))
                self.assertIn("invalid numeric field: upstream_max_input_tokens", payload["error"]["message"])
                saved = gateway.load_config()
                self.assertEqual(saved["upstream"].get("base_url"), cfg["upstream"].get("base_url"))
                self.assertEqual(saved["gateway"]["max_concurrent_requests"], 32)
                self.assertEqual(saved["context"]["max_input_tokens"], 24000)

                bad_profile = urllib.parse.urlencode(
                    {
                        "action": "save",
                        "name": "bad-profile",
                        "base_url": "http://profile-mutated.local",
                        "model": "profile-model",
                        "protocol": "openai_chat",
                        "upstream_timeout_seconds": "bad-timeout",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    base + "/admin/upstream-profile",
                    data=bad_profile,
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as profile_error:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(profile_error.exception.code, 400)
                payload = json.loads(profile_error.exception.read().decode("utf-8"))
                self.assertIn("invalid numeric field: upstream_timeout_seconds", payload["error"]["message"])
                saved = gateway.load_config()
                self.assertFalse(any(p.get("name") == "bad-profile" for p in saved.get("upstream_profiles", [])))

                bad_client = urllib.parse.urlencode(
                    {
                        "public_base_url": "http://mutated.local:8885",
                        "client_snippet_api_key": "mutated-key",
                        "client_context_window": "1000000",
                        "client_auto_compact_token_limit": "bad-limit",
                        "client_output_token_limit": "128000",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    base + "/admin/client-config",
                    data=bad_client,
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as client_error:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(client_error.exception.code, 400)
                payload = json.loads(client_error.exception.read().decode("utf-8"))
                self.assertIn("invalid numeric field: client_auto_compact_token_limit", payload["error"]["message"])
                saved = gateway.load_config()
                self.assertEqual(saved["gateway"]["public_base_url"], "http://before.local:8885")
                self.assertEqual(saved["gateway"]["client_snippet_api_key"], "before-key")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_admin_numeric_form_preserves_existing_values_when_fields_are_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = "http://before-upstream.local"
                cfg["upstream"]["model"] = "before-model"
                cfg["upstream"]["timeout_seconds"] = 12.5
                cfg["upstream"]["max_input_tokens"] = 111111
                cfg["upstream"]["max_output_tokens"] = 2222
                cfg["upstream"]["max_concurrency"] = 7
                cfg["gateway"]["max_tool_rounds"] = 9
                cfg["gateway"]["max_concurrent_requests"] = 11
                cfg["gateway"]["concurrency_queue_timeout_seconds"] = 2.5
                cfg["gateway"]["tool_execution_timeout_seconds"] = 33.5
                cfg["gateway"]["text_tool_adapter_compact_token_limit"] = 7777
                cfg["gateway"]["public_base_url"] = "http://before.local:8885"
                cfg["gateway"]["client_snippet_api_key"] = "before-key"
                cfg["gateway"]["client_context_window"] = 123456
                cfg["gateway"]["client_auto_compact_token_limit"] = 120000
                cfg["gateway"]["client_output_token_limit"] = 4096
                cfg["context"]["max_input_tokens"] = 34567
                cfg["context"]["fanout_chunk_tokens"] = 4567
                cfg["context"]["fanout_max_chunks"] = 3
                cfg["context"]["fanout_max_workers"] = 2
                gateway.save_config(cfg)
                token = base64.b64encode(b"admin:admin").decode("ascii")
                base = f"http://127.0.0.1:{httpd.server_address[1]}"

                partial_config = urllib.parse.urlencode(
                    {
                        "base_url": "http://after-upstream.local",
                        "model": "after-model",
                        "protocol": "anthropic_messages",
                        "tool_mode": "orchestrate",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    base + "/admin/config",
                    data=partial_config,
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=5).read()
                except Exception as exc:
                    if getattr(exc, "code", None) != 303:
                        raise
                saved = gateway.load_config()
                self.assertEqual(saved["upstream"]["base_url"], "http://after-upstream.local")
                self.assertEqual(saved["upstream"]["timeout_seconds"], 12.5)
                self.assertEqual(saved["upstream"]["max_input_tokens"], 111111)
                self.assertEqual(saved["upstream"]["max_output_tokens"], 2222)
                self.assertEqual(saved["upstream"]["max_concurrency"], 7)
                self.assertEqual(saved["gateway"]["max_tool_rounds"], 9)
                self.assertEqual(saved["gateway"]["max_concurrent_requests"], 11)
                self.assertEqual(saved["gateway"]["concurrency_queue_timeout_seconds"], 2.5)
                self.assertEqual(saved["gateway"]["tool_execution_timeout_seconds"], 33.5)
                self.assertEqual(saved["gateway"]["text_tool_adapter_compact_token_limit"], 7777)
                self.assertEqual(saved["context"]["max_input_tokens"], 34567)
                self.assertEqual(saved["context"]["fanout_chunk_tokens"], 4567)
                self.assertEqual(saved["context"]["fanout_max_chunks"], 3)
                self.assertEqual(saved["context"]["fanout_max_workers"], 2)

                partial_client = urllib.parse.urlencode(
                    {
                        "public_base_url": "http://after.local:8885",
                        "client_snippet_api_key": "after-key",
                        "downstream_model_alias": "after-model",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    base + "/admin/client-config",
                    data=partial_client,
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=5).read()
                except Exception as exc:
                    if getattr(exc, "code", None) != 303:
                        raise
                saved = gateway.load_config()
                self.assertEqual(saved["gateway"]["public_base_url"], "http://after.local:8885")
                self.assertEqual(saved["gateway"]["client_snippet_api_key"], "after-key")
                self.assertEqual(saved["gateway"]["client_context_window"], 123456)
                self.assertEqual(saved["gateway"]["client_auto_compact_token_limit"], 120000)
                self.assertEqual(saved["gateway"]["client_output_token_limit"], 4096)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_admin_post_rejects_cross_origin_browser_request(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                gateway.save_config(gateway._default_config())
                token = base64.b64encode(b"admin:admin").decode("ascii")
                form = urllib.parse.urlencode(
                    {
                        "public_base_url": "http://attacker-controlled.local",
                        "client_snippet_api_key": "should-not-save",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/client-config",
                    data=form,
                    headers={
                        "authorization": f"Basic {token}",
                        "content-type": "application/x-www-form-urlencoded",
                        "Origin": "https://evil.example",
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(err.exception.code, 403)
                payload = json.loads(err.exception.read().decode("utf-8"))
                self.assertIn("cross-origin admin request rejected", payload["error"]["message"])
                saved = gateway.load_config()
                self.assertNotEqual(saved["gateway"]["public_base_url"], "http://attacker-controlled.local")
                self.assertNotEqual(saved["gateway"]["client_snippet_api_key"], "should-not-save")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_admin_post_rejects_malformed_origin_without_mutating_config(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                gateway.save_config(gateway._default_config())
                token = base64.b64encode(b"admin:admin").decode("ascii")
                form = urllib.parse.urlencode(
                    {
                        "public_base_url": "http://malformed-origin.local",
                        "client_snippet_api_key": "should-not-save",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/client-config",
                    data=form,
                    headers={
                        "authorization": f"Basic {token}",
                        "content-type": "application/x-www-form-urlencoded",
                        "Origin": "http://127.0.0.1:bad-port",
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(err.exception.code, 403)
                payload = json.loads(err.exception.read().decode("utf-8"))
                self.assertIn("cross-origin admin request rejected", payload["error"]["message"])
                saved = gateway.load_config()
                self.assertNotEqual(saved["gateway"]["public_base_url"], "http://malformed-origin.local")
                self.assertNotEqual(saved["gateway"]["client_snippet_api_key"], "should-not-save")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_admin_post_allows_same_origin_browser_request(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                gateway.save_config(gateway._default_config())
                token = base64.b64encode(b"admin:admin").decode("ascii")
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                form = urllib.parse.urlencode(
                    {
                        "public_base_url": "http://same-origin.local:8885",
                        "client_snippet_api_key": "same-origin-key",
                        "downstream_model_alias": "gpt-5.4",
                        "review_model_alias": "gpt-5.4",
                        "codex_reasoning_effort": "xhigh",
                        "client_context_window": "1000000",
                        "client_auto_compact_token_limit": "900000",
                        "client_output_token_limit": "128000",
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    base + "/admin/client-config",
                    data=form,
                    headers={
                        "authorization": f"Basic {token}",
                        "content-type": "application/x-www-form-urlencoded",
                        "Origin": base,
                    },
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=5).read()
                except Exception as exc:
                    if getattr(exc, "code", None) != 303:
                        raise
                saved = gateway.load_config()
                self.assertEqual(saved["gateway"]["public_base_url"], "http://same-origin.local:8885")
                self.assertEqual(saved["gateway"]["client_snippet_api_key"], "same-origin-key")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_api_post_rejects_body_over_configured_limit(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["max_request_body_bytes"] = 32
                gateway.save_config(cfg)
                body = json.dumps({"name": "calculator", "arguments": {"expression": "1+1"}, "padding": "x" * 128}).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/v1/tools/call",
                    data=body,
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(err.exception.code, 413)
                payload = json.loads(err.exception.read().decode("utf-8"))
                self.assertIn("request body too large", payload["error"]["message"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_admin_post_rejects_large_form_without_mutating_config(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["max_request_body_bytes"] = 48
                gateway.save_config(cfg)
                token = base64.b64encode(b"admin:admin").decode("ascii")
                form = urllib.parse.urlencode(
                    {
                        "public_base_url": "http://oversized.example",
                        "client_snippet_api_key": "oversized-key",
                        "padding": "x" * 256,
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/client-config",
                    data=form,
                    headers={
                        "authorization": f"Basic {token}",
                        "content-type": "application/x-www-form-urlencoded",
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(err.exception.code, 413)
                payload = json.loads(err.exception.read().decode("utf-8"))
                self.assertIn("request body too large", payload["error"]["message"])
                saved = gateway.load_config()
                self.assertNotEqual(saved["gateway"].get("public_base_url"), "http://oversized.example")
                self.assertNotEqual(saved["gateway"].get("client_snippet_api_key"), "oversized-key")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_downstream_key_protocol_restrictions_include_models_compatibility(self):
        class DummyHandler:
            def __init__(self, path: str, key: str):
                self.path = path
                self.headers = {"authorization": f"Bearer {key}"}

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["downstream_keys"] = [
                    {
                        "name": "chat-only",
                        "key_hash": gateway._hash_secret("chat-only-key"),
                        "prefix": "chat-onl",
                        "enabled": True,
                        "protocols": ["chat_completions"],
                    },
                    {
                        "name": "tools-only",
                        "key_hash": gateway._hash_secret("tools-only-key"),
                        "prefix": "tools-on",
                        "enabled": True,
                        "protocols": ["direct_tools"],
                    },
                ]
                gateway.save_config(cfg)
                self.assertEqual(gateway._check_downstream_key(DummyHandler("/v1/models", "chat-only-key")), "chat-only")
                self.assertEqual(gateway._check_downstream_key(DummyHandler("/v1/tools/call", "tools-only-key")), "tools-only")
                with self.assertRaises(gateway.DownstreamAuthError):
                    gateway._check_downstream_key(DummyHandler("/v1/responses", "chat-only-key"))
                with self.assertRaises(gateway.DownstreamAuthError):
                    gateway._check_downstream_key(DummyHandler("/v1/tools/call", "chat-only-key"))
            finally:
                gateway.CONFIG_PATH = old_config

    def test_anthropic_base_url_prefix_routes_to_messages(self):
        class ChatOnlyHandler(BaseHTTPRequestHandler):
            seen: list[dict] = []

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))).decode("utf-8"))
                ChatOnlyHandler.seen.append({"path": self.path, "body": body})
                payload = json.dumps(
                    {
                        "id": "chatcmpl_alias",
                        "object": "chat.completion",
                        "model": body.get("model") or "m",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):  # noqa: N802
                pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), ChatOnlyHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
                cfg["upstream"]["model"] = "mimo-v2.5-pro"
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["downstream_keys"] = [
                    {
                        "name": "claude",
                        "key_hash": gateway._hash_secret("test-gateway-key"),
                        "prefix": "test-gat",
                        "enabled": True,
                        "protocols": ["messages"],
                    }
                ]
                gateway.save_config(cfg)
                body = json.dumps(
                    {
                        "model": "mimo-v2.5-pro",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/anthropic/v1/messages",
                    data=body,
                    headers={"authorization": "Bearer test-gateway-key", "content-type": "application/json"},
                    method="POST",
                )
                response = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
                self.assertEqual(response["id"], "chatcmpl_alias")
                self.assertEqual(response["type"], "message")
                self.assertEqual(response["role"], "assistant")
                self.assertEqual(response["model"], "mimo-v2.5-pro")
                self.assertEqual(response["content"][0]["text"], "ok")
                self.assertEqual(response["stop_reason"], "end_turn")
                self.assertIsNone(response["stop_sequence"])
                self.assertEqual(response["usage"]["input_tokens"], 0)
                self.assertEqual(response["usage"]["output_tokens"], 0)
                self.assertEqual(ChatOnlyHandler.seen[-1]["path"], "/v1/chat/completions")
                self.assertEqual(ChatOnlyHandler.seen[-1]["body"]["messages"][-1]["content"], "hello")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_anthropic_prefix_accepts_x_api_key_auth(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["downstream_keys"] = [
                    {
                        "name": "anthropic-sdk",
                        "key_hash": gateway._hash_secret("test-gateway-key"),
                        "prefix": "test-gat",
                        "enabled": True,
                        "protocols": ["messages"],
                    }
                ]
                gateway.save_config(cfg)
                body = json.dumps(
                    {
                        "model": "mimo-v2.5-pro",
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/anthropic/v1/messages/count_tokens",
                    data=body,
                    headers={"x-api-key": "test-gateway-key", "content-type": "application/json"},
                    method="POST",
                )
                response = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
                self.assertGreater(response["input_tokens"], 0)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_anthropic_prefix_token_count_routes_to_canonical_messages_count(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["downstream_keys"] = [
                    {
                        "name": "claude",
                        "key_hash": gateway._hash_secret("test-gateway-key"),
                        "prefix": "test-gat",
                        "enabled": True,
                        "protocols": ["messages"],
                    }
                ]
                gateway.save_config(cfg)
                body = json.dumps(
                    {
                        "model": "mimo-v2.5-pro",
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/anthropic/v1/messages/count_tokens",
                    data=body,
                    headers={"authorization": "Bearer test-gateway-key", "content-type": "application/json"},
                    method="POST",
                )
                response = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
                self.assertGreater(response["input_tokens"], 0)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_downstream_key_is_enforced_for_post_routes(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["downstream_keys"] = [
                    {
                        "name": "tools",
                        "key_hash": gateway._hash_secret("tools-key"),
                        "prefix": "tools-ke",
                        "enabled": True,
                        "protocols": ["direct_tools"],
                    }
                ]
                gateway.save_config(cfg)
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                body = json.dumps(
                    {
                        "name": "calculator",
                        "arguments": {"expression": "1+2"},
                    }
                ).encode("utf-8")
                unauth = urllib.request.Request(
                    base + "/v1/tools/call",
                    data=body,
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(unauth, timeout=5).read()
                self.assertEqual(cm.exception.code, 401)

                malformed_unauth = urllib.request.Request(
                    base + "/v1/tools/call",
                    data=b"{",
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(malformed_unauth, timeout=5).read()
                self.assertEqual(cm.exception.code, 401)

                cfg["gateway"]["max_request_body_bytes"] = 16
                gateway.save_config(cfg)
                oversized_unauth = urllib.request.Request(
                    base + "/v1/tools/call",
                    data=json.dumps({"padding": "x" * 128}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(oversized_unauth, timeout=5).read()
                self.assertEqual(cm.exception.code, 401)

                cfg["gateway"]["max_request_body_bytes"] = 64 * 1024 * 1024
                gateway.save_config(cfg)
                valid = urllib.request.Request(
                    base + "/v1/tools/call",
                    data=body,
                    headers={"content-type": "application/json", "authorization": "Bearer tools-key"},
                    method="POST",
                )
                payload = json.loads(urllib.request.urlopen(valid, timeout=5).read().decode("utf-8"))
                self.assertTrue(payload["success"])
                self.assertEqual(payload["content"], "3")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_fanout_synthesis_prompt_does_not_resend_full_original(self):
        original = "分析这套项目\n" + ("README line xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n" * 2000)
        partials = ["partial " + str(i) + " " + ("evidence " * 1000) for i in range(8)]
        prompt = gateway._make_synthesis_prompt(original, partials)
        review = gateway._make_quality_review_prompt(original, "draft " * 10000)
        self.assertLess(len(prompt), 42000)
        self.assertLess(len(review), 18000)
        self.assertIn("原始用户问题（压缩）", prompt)
        self.assertIn("[gateway context compacted]", prompt)
        self.assertNotIn("Sorry, the text you sent is too long", prompt)

    def test_upstream_too_long_response_triggers_forced_fanout(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["context"]["enabled"] = True
                cfg["context"]["fanout_enabled"] = True
                cfg["context"]["fanout_chunk_tokens"] = 120
                cfg["context"]["fanout_max_workers"] = 2
                cfg["context"]["quality_review_enabled"] = False
                gateway.save_config(cfg)
                client = FakeClient([
                    {"choices": [{"message": {"content": "Sorry, the text you sent is too long!"}}]},
                    {"choices": [{"message": {"content": "part one"}}]},
                    {"choices": [{"message": {"content": "part two"}}]},
                    {"choices": [{"message": {"content": "part three"}}]},
                    {"choices": [{"message": {"content": "part four"}}]},
                    {"choices": [{"message": {"content": "final synthesis"}}]},
                ])
                large = "分析这套项目\n" + ("class A {}\n" * 300)
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "m", "messages": [{"role": "user", "content": large}]},
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "final synthesis")
                self.assertEqual(final["gateway_context"]["strategy"], "fanout_forced_synthesis")
                self.assertGreaterEqual(final["gateway_context"]["chunks"], 2)
                self.assertGreaterEqual(len(client.requests), 4)
            finally:
                gateway.CONFIG_PATH = old_config

    def test_fanout_max_chunks_zero_keeps_all_chunks(self):
        chunks = gateway._chunk_text_by_tokens("\n\n".join(f"section {i} " + ("x" * 1200) for i in range(5)), 250, 0)
        self.assertGreaterEqual(len(chunks), 5)

    def test_logging_defaults_to_sqlite_without_jsonl_writes(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_request_log = gateway.REQUEST_LOG_PATH
            old_stats = gateway.STATS_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite_env = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            old_failure_env = os.environ.get("GATEWAY_TOOL_FAILURE_LOG")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            gateway.REQUEST_LOG_PATH = pathlib.Path(td) / "requests.jsonl"
            gateway.STATS_PATH = pathlib.Path(td) / "stats.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "gateway.sqlite3")
            os.environ["GATEWAY_TOOL_FAILURE_LOG"] = str(pathlib.Path(td) / "failures.jsonl")
            gateway.SQLITE_READY = False
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["logging_backend"] = "sqlite"
                gateway.save_config(cfg)
                missing = _execute_tool_call(ToolCall("missing", "not_a_real_tool", {}, {}))
                self.assertFalse(missing.success)
                gateway._record_request_stat("/v1/messages", 200)
                gateway._write_request_log("/v1/messages", {"messages": []}, 200, {"ok": True}, "test-key")
                self.assertTrue(pathlib.Path(os.environ["GATEWAY_SQLITE_LOG_PATH"]).exists())
                self.assertFalse(gateway.REQUEST_LOG_PATH.exists())
                self.assertFalse(pathlib.Path(os.environ["GATEWAY_TOOL_FAILURE_LOG"]).exists())
                self.assertFalse(gateway.STATS_PATH.exists())
                stats = gateway._stats_snapshot()
                self.assertEqual(stats["backend"], "sqlite")
                self.assertGreaterEqual(stats["requests"]["total"], 1)
                self.assertIn("not_a_real_tool", {row["tool_name"] for row in gateway._tail_failures(20)})
                self.assertEqual(gateway._tail_requests(20)[-1]["path"], "/v1/messages")
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.REQUEST_LOG_PATH = old_request_log
                gateway.STATS_PATH = old_stats
                gateway.SQLITE_READY = old_ready
                if old_sqlite_env is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite_env
                if old_failure_env is None:
                    os.environ.pop("GATEWAY_TOOL_FAILURE_LOG", None)
                else:
                    os.environ["GATEWAY_TOOL_FAILURE_LOG"] = old_failure_env

    def test_request_log_truncates_large_payloads(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_request_log = gateway.REQUEST_LOG_PATH
            old_stats = gateway.STATS_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite_env = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            gateway.REQUEST_LOG_PATH = pathlib.Path(td) / "requests.jsonl"
            gateway.STATS_PATH = pathlib.Path(td) / "stats.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "gateway.sqlite3")
            gateway.SQLITE_READY = False
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["logging_backend"] = "sqlite"
                cfg["gateway"]["max_log_payload_chars"] = 120
                gateway.save_config(cfg)
                large_text = "x" * 1000
                gateway._write_request_log(
                    "/v1/messages",
                    {"messages": [{"role": "user", "content": large_text}]},
                    200,
                    {"content": [{"type": "text", "text": large_text}]},
                    "test-key",
                )
                row = gateway._tail_requests(1)[0]
                self.assertTrue(row["request"].get("gateway_truncated"))
                self.assertTrue(row["response"].get("gateway_truncated"))
                request_json = json.dumps(row["request"], ensure_ascii=False)
                response_json = json.dumps(row["response"], ensure_ascii=False)
                self.assertLessEqual(len(request_json), 220)
                self.assertLessEqual(len(response_json), 220)
                self.assertIn("omitted", request_json)
                self.assertNotIn(large_text, request_json)
                self.assertNotIn(large_text, response_json)
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.REQUEST_LOG_PATH = old_request_log
                gateway.STATS_PATH = old_stats
                gateway.SQLITE_READY = old_ready
                if old_sqlite_env is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite_env

    def test_request_log_redacts_common_secret_fields(self):
        redacted = gateway._redact_payload(
            {
                "Authorization": "Bearer live-token",
                "headers": {
                    "X-API-Key": "header-key",
                    "Cookie": "session=secret",
                    "content-type": "application/json",
                },
                "metadata": {
                    "apiKey": "camel-key",
                    "access_token": "access-token",
                    "refresh-token": "refresh-token",
                    "client_secret": "client-secret",
                    "password": "plain-password",
                    "max_output_tokens": 8192,
                },
                "items": [{"set-cookie": "sid=secret"}, {"normal": "visible"}],
            }
        )

        self.assertEqual(redacted["Authorization"], "***")
        self.assertEqual(redacted["headers"]["X-API-Key"], "***")
        self.assertEqual(redacted["headers"]["Cookie"], "***")
        self.assertEqual(redacted["headers"]["content-type"], "application/json")
        self.assertEqual(redacted["metadata"]["apiKey"], "***")
        self.assertEqual(redacted["metadata"]["access_token"], "***")
        self.assertEqual(redacted["metadata"]["refresh-token"], "***")
        self.assertEqual(redacted["metadata"]["client_secret"], "***")
        self.assertEqual(redacted["metadata"]["password"], "***")
        self.assertEqual(redacted["metadata"]["max_output_tokens"], 8192)
        self.assertEqual(redacted["items"][0]["set-cookie"], "***")
        self.assertEqual(redacted["items"][1]["normal"], "visible")

    def test_redacted_config_covers_nested_secrets_and_key_hashes(self):
        cfg = gateway._default_config()
        cfg["admin"]["password_hash"] = "admin-hash"
        cfg["upstream"]["api_key"] = "upstream-key"
        cfg["upstream_profiles"] = [
            {
                "name": "primary",
                "api_key": "profile-key",
                "headers": {"x-api-key": "profile-header-key"},
            }
        ]
        cfg["context"]["long_context_upstream"]["api_key"] = "long-context-key"
        cfg["downstream_keys"] = [{"name": "client", "key_hash": "downstream-hash", "prefix": "client-p"}]
        cfg["http_actions"]["actions"] = [
            {
                "name": "callback",
                "headers": {"Authorization": "Bearer action-token", "Cookie": "sid=secret"},
                "client_secret": "action-secret",
            }
        ]

        redacted = gateway._redacted_config(cfg)

        self.assertNotIn("password_hash", redacted["admin"])
        self.assertIs(redacted["admin"]["must_change_password"], cfg["admin"]["must_change_password"])
        self.assertEqual(redacted["upstream"]["api_key"], "***")
        self.assertEqual(redacted["upstream_profiles"][0]["api_key"], "***")
        self.assertEqual(redacted["upstream_profiles"][0]["headers"]["x-api-key"], "***")
        self.assertEqual(redacted["context"]["long_context_upstream"]["api_key"], "***")
        self.assertEqual(redacted["downstream_keys"][0]["key_hash"], "***")
        self.assertEqual(redacted["downstream_keys"][0]["prefix"], "client-p")
        self.assertEqual(redacted["http_actions"]["actions"][0]["headers"]["Authorization"], "***")
        self.assertEqual(redacted["http_actions"]["actions"][0]["headers"]["Cookie"], "***")
        self.assertEqual(redacted["http_actions"]["actions"][0]["client_secret"], "***")

    def test_file_logging_backend_is_readonly_unless_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_request_log = gateway.REQUEST_LOG_PATH
            old_stats = gateway.STATS_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite_env = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            old_allow_file = os.environ.get("GATEWAY_ALLOW_FILE_LOGGING")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            gateway.REQUEST_LOG_PATH = pathlib.Path(td) / "requests.jsonl"
            gateway.STATS_PATH = pathlib.Path(td) / "stats.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "gateway.sqlite3")
            os.environ.pop("GATEWAY_ALLOW_FILE_LOGGING", None)
            gateway.SQLITE_READY = False
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["logging_backend"] = "jsonl"
                gateway.save_config(cfg)
                self.assertEqual(gateway._logging_backend(), "sqlite")
                gateway._record_request_stat("/v1/messages", 200)
                gateway._write_request_log("/v1/messages", {"messages": []}, 200, {"ok": True}, "test-key")
                self.assertTrue(pathlib.Path(os.environ["GATEWAY_SQLITE_LOG_PATH"]).exists())
                self.assertFalse(gateway.REQUEST_LOG_PATH.exists())
                self.assertFalse(gateway.STATS_PATH.exists())
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.REQUEST_LOG_PATH = old_request_log
                gateway.STATS_PATH = old_stats
                gateway.SQLITE_READY = old_ready
                if old_sqlite_env is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite_env
                if old_allow_file is None:
                    os.environ.pop("GATEWAY_ALLOW_FILE_LOGGING", None)
                else:
                    os.environ["GATEWAY_ALLOW_FILE_LOGGING"] = old_allow_file

    def test_orchestrates_responses_until_final(self):
        old_protocol = os.environ.get("UPSTREAM_PROTOCOL")
        os.environ["UPSTREAM_PROTOCOL"] = "openai_chat"
        try:
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
            self.assertEqual(final["object"], "response")
            self.assertTrue(final["id"].startswith("resp_"))
            self.assertEqual(final["status"], "completed")
            self.assertIn("usage", final)
            self.assertEqual(final["output"][0]["type"], "message")
            # Upstream request is in OpenAI Chat format, tool result content is a string
            self.assertEqual(client.requests[1][1]["messages"][-1]["content"], "10")
        finally:
            if old_protocol:
                os.environ["UPSTREAM_PROTOCOL"] = old_protocol
            else:
                os.environ.pop("UPSTREAM_PROTOCOL", None)

    def test_responses_custom_tool_call_executes_and_appends_custom_output(self):
        response = {
            "output": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call_custom_1",
                    "name": "calculator",
                    "input": "40+2",
                }
            ]
        }
        self.assertTrue(_native_tool_signal("/v1/responses", response))
        calls = _extract_tool_calls("/v1/responses", response)
        self.assertEqual(calls[0].name, "calculator")
        self.assertEqual(calls[0].arguments["input"], "40+2")
        result = _execute_tool_call(calls[0])
        self.assertTrue(result.success)
        updated = _append_tool_results("/v1/responses", {"input": "calc"}, response, [result])
        self.assertEqual(updated["input"][-1]["type"], "custom_tool_call_output")
        self.assertEqual(updated["input"][-1]["call_id"], "call_custom_1")
        self.assertEqual(updated["input"][-1]["output"], "42")

    def test_responses_like_empty_output_gets_strict_shape(self):
        from src.gateway_protocol import _convert_response_to_downstream
        payload = _convert_response_to_downstream("/v1/responses", {"object": "response", "output": []}, "openai_chat")
        self.assertEqual(payload["object"], "response")
        self.assertTrue(payload["id"].startswith("resp_"))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output"], [])
        self.assertEqual(payload["usage"], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

    def test_codex_responses_orchestrates_calc_alias_expr_until_final(self):
        old_protocol = os.environ.get("UPSTREAM_PROTOCOL")
        os.environ["UPSTREAM_PROTOCOL"] = "openai_chat"
        try:
            client = FakeClient(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_calc_alias",
                                "name": "calc",
                                "arguments": "{\"expr\":\"2+2\"}",
                            }
                        ]
                    },
                    {"output": [{"type": "message", "content": [{"type": "output_text", "text": "4"}]}]},
                ]
            )
            final = run_tool_orchestration("/v1/responses", {"model": "m", "input": "What is 2+2?"}, client)
            self.assertEqual(final["object"], "response")
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["output"][0]["content"][0]["text"], "4")
            second_request = client.requests[1][1]
            serialized = json.dumps(second_request, ensure_ascii=False)
            self.assertIn('"role": "tool"', serialized)
            self.assertIn('"tool_call_id": "call_calc_alias"', serialized)
            self.assertIn('"content": "4"', serialized)
            self.assertNotIn("tool_not_found", serialized)
        finally:
            if old_protocol:
                os.environ["UPSTREAM_PROTOCOL"] = old_protocol
            else:
                os.environ.pop("UPSTREAM_PROTOCOL", None)

    def test_claude_messages_orchestrates_calc_alias_expr_until_final(self):
        client = FakeClient(
            [
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_calc_alias",
                            "name": "calc",
                            "input": {"expr": "2+2"},
                        }
                    ],
                    "stop_reason": "tool_use",
                },
                {"content": [{"type": "text", "text": "4"}], "stop_reason": "end_turn"},
            ]
        )
        final = run_tool_orchestration(
            "/v1/messages",
            {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "What is 2+2?"}]},
            client,
        )
        self.assertEqual(final["content"][0]["text"], "4")
        second_request = client.requests[1][1]
        serialized = json.dumps(second_request, ensure_ascii=False)
        self.assertIn("4", serialized)
        self.assertNotIn("tool_not_found", serialized)

    def test_orchestrates_messages_until_final(self):
        # Set upstream to OpenAI Chat to match test expectation
        old_protocol = os.environ.get("UPSTREAM_PROTOCOL")
        os.environ["UPSTREAM_PROTOCOL"] = "openai_chat"
        try:
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
            # Upstream request is in OpenAI Chat format, tool result content is a string
            self.assertEqual(client.requests[1][1]["messages"][-1]["content"], "8")
        finally:
            if old_protocol:
                os.environ["UPSTREAM_PROTOCOL"] = old_protocol
            else:
                os.environ.pop("UPSTREAM_PROTOCOL", None)

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
                cfg = gateway._default_config()
                cfg["mcp"] = {
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
                }
                cfg["upstream"]["capabilities"]["supports_tools"] = True
                cfg["upstream"]["capabilities"]["supports_function_calls"] = True
                gateway.save_config(cfg)
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
                legacy_name = _mcp_legacy_public_name("test", "echo_mcp")
                # Legacy format mcp_server_tool is ambiguous; only mcp__server__tool is parsed
                self.assertIsNone(_mcp_parse_public_name(legacy_name))
                self.assertEqual(_mcp_parse_public_name(public_name), ("test", "echo_mcp"))
                merged = _merge_builtin_tools("/v1/chat/completions", {"messages": []})
                names = [
                    t.get("function", {}).get("name")
                    for t in merged.get("tools", [])
                    if isinstance(t, dict) and isinstance(t.get("function"), dict)
                ]
                self.assertIn(public_name, names)
                self.assertIn(legacy_name, names)
            finally:
                gateway._mcp_close_sessions()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_mcp_resource_helper_tools_are_real_not_placeholders(self):
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
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{"resources":{},"prompts":{}},"serverInfo":{"name":"fake","version":"1"}}})
    elif method == "resources/list":
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"resources":[{"uri":"file:///demo.txt","name":"demo"}]}})
    elif method == "resources/templates/list":
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"resourceTemplates":[{"uriTemplate":"file:///{name}.txt","name":"tpl"}]}})
    elif method == "resources/read":
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"contents":[{"uri":msg["params"]["uri"],"text":"resource body"}]}})
    elif method == "prompts/get":
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"messages":[{"role":"user","content":{"type":"text","text":"prompt body"}}]}})
    else:
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{}})
'''
        with tempfile.TemporaryDirectory() as td:
            script_path = pathlib.Path(td) / "fake_mcp_resources.py"
            script_path.write_text(script, encoding="utf-8")
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                cfg = gateway._default_config()
                cfg["mcp"] = {
                    "enabled": True,
                    "servers": [
                        {
                            "name": "res",
                            "command": sys.executable,
                            "args": [str(script_path)],
                            "cwd": td,
                            "enabled": True,
                        }
                    ],
                }
                gateway.save_config(cfg)
                resources = _execute_tool_call(ToolCall("lr", "list_mcp_resources", {"server": "res"}, {}))
                self.assertTrue(resources.success)
                self.assertIn("file:///demo.txt", resources.content)
                templates = _execute_tool_call(ToolCall("lt", "list_mcp_resource_templates", {"server": "res"}, {}))
                self.assertTrue(templates.success)
                self.assertIn("uriTemplate", templates.content)
                read = _execute_tool_call(ToolCall("rr", "read_mcp_resource", {"server": "res", "uri": "file:///demo.txt"}, {}))
                self.assertTrue(read.success)
                self.assertIn("resource body", read.content)
                prompt = _execute_tool_call(ToolCall("gp", "mcp_get_prompt", {"server": "res", "name": "p"}, {}))
                self.assertTrue(prompt.success)
                self.assertIn("prompt body", prompt.content)
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
                        "upstream": {
                            **gateway._default_config().get("upstream", {}),
                            "tools_enabled": "native",
                        },
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

    def test_http_action_get_uses_query_and_expands_env_headers(self):
        class EchoHandler(BaseHTTPRequestHandler):
            seen = []

            def log_message(self, fmt, *args):
                return

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                EchoHandler.seen.append(
                    {
                        "path": parsed.path,
                        "query": urllib.parse.parse_qs(parsed.query),
                        "authorization": self.headers.get("authorization"),
                    }
                )
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_token = os.environ.get("LOOKUP_TOKEN")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["LOOKUP_TOKEN"] = "Bearer env-token"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [
                        {
                            "name": "lookup_http",
                            "description": "Lookup through HTTP action",
                            "method": "GET",
                            "url": f"http://127.0.0.1:{httpd.server_address[1]}/lookup",
                            "headers": {"authorization": "${LOOKUP_TOKEN}"},
                        }
                    ],
                }
                gateway.save_config(cfg)
                result = _execute_tool_call(
                    ToolCall("http_get", "lookup_http", {"id": "42", "active": True, "leak": "${LOOKUP_TOKEN}"}, {})
                )
                self.assertTrue(result.success)
                self.assertEqual(EchoHandler.seen[0]["path"], "/lookup")
                self.assertEqual(
                    EchoHandler.seen[0]["query"],
                    {"id": ["42"], "active": ["true"], "leak": ["${LOOKUP_TOKEN}"]},
                )
                self.assertEqual(EchoHandler.seen[0]["authorization"], "Bearer env-token")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_token is None:
                    os.environ.pop("LOOKUP_TOKEN", None)
                else:
                    os.environ["LOOKUP_TOKEN"] = old_token

    def test_http_action_http_error_records_tool_failure(self):
        class FailingHandler(BaseHTTPRequestHandler):
            calls = 0

            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                FailingHandler.calls += 1
                payload = b'{"error":"bad upstream"}'
                self.send_response(503)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_sqlite_env = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            old_ready = gateway.SQLITE_READY
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "trace.sqlite3")
            gateway.SQLITE_READY = False
            gateway._sqlite_init()
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), FailingHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [
                        {
                            "name": "failing_http",
                            "method": "POST",
                            "url": f"http://127.0.0.1:{httpd.server_address[1]}/fail",
                        }
                    ],
                }
                gateway.save_config(cfg)
                result = _execute_tool_call(ToolCall("http_fail", "failing_http", {"value": "x"}, {}), provider="test")
                self.assertFalse(result.success)
                self.assertEqual(result.failure_type, "http_action_failed")
                self.assertIn("HTTP 503", result.content)
                self.assertEqual(FailingHandler.calls, 1)
                failures = gateway._tail_failures(10)
                self.assertTrue(any(f.get("tool_name") == "failing_http" and f.get("failure_type") == "http_action_failed" for f in failures))
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                gateway.SQLITE_READY = old_ready
                if old_sqlite_env is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite_env

    def test_http_action_response_max_bytes_is_enforced(self):
        class LargeHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                payload = b"x" * 64
                self.send_response(200)
                self.send_header("content-type", "text/plain")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), LargeHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [
                        {
                            "name": "large_http",
                            "method": "POST",
                            "url": f"http://127.0.0.1:{httpd.server_address[1]}/large",
                            "max_bytes": 16,
                        }
                    ],
                }
                gateway.save_config(cfg)
                result = _execute_tool_call(ToolCall("http_large", "large_http", {}, {}))
                self.assertFalse(result.success)
                self.assertEqual(result.failure_type, "response_too_large")
                self.assertIn("exceeded max_bytes", result.content)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_http_action_invalid_url_fails_as_tool_failure(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [{"name": "bad_http", "method": "POST", "url": "file:///etc/passwd"}],
                }
                gateway.save_config(cfg)
                result = _execute_tool_call(ToolCall("http_bad", "bad_http", {}, {}))
                self.assertFalse(result.success)
                self.assertIn(result.failure_type, {"invalid_input", "http_action_failed"})
            finally:
                gateway.CONFIG_PATH = old_config

    def test_streaming_chat_request_returns_sse_without_upstream_stream_in_orchestrate_mode(self):
        class UpstreamHandler(BaseHTTPRequestHandler):
            seen_bodies = []

            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                UpstreamHandler.seen_bodies.append(body)
                payload = json.dumps(
                    {
                        "id": "chatcmpl_test",
                        "model": "m",
                        "choices": [{"message": {"role": "assistant", "content": "stream ok"}}],
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_force = os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = "0"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
            gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            upstream_thread.start()
            gateway_thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
                cfg["upstream"]["model"] = "m"
                cfg["gateway"]["tool_mode"] = "orchestrate"
                gateway.save_config(cfg)
                req = urllib.request.Request(
                    f"http://127.0.0.1:{gateway_server.server_address[1]}/v1/chat/completions",
                    data=json.dumps(
                        {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
                    ).encode("utf-8"),
                    headers={"authorization": "Bearer local-gateway-key", "content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
                    text = resp.read().decode("utf-8")
                self.assertIn("stream ok", text)
                self.assertIn("data: [DONE]", text)
                self.assertFalse(UpstreamHandler.seen_bodies[0]["stream"])
                self.assertEqual(_response_text("/v1/chat/completions", {"choices": [{"message": {"content": "x"}}]}), "x")
            finally:
                upstream.shutdown()
                gateway_server.shutdown()
                upstream.server_close()
                gateway_server.server_close()
                upstream_thread.join(timeout=2)
                gateway_thread.join(timeout=2)
                gateway._mcp_close_sessions()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_force is None:
                    os.environ.pop("GATEWAY_UPSTREAM_STREAM_AGGREGATE", None)
                else:
                    os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = old_force

    def test_streaming_chat_request_passthrough_proxies_upstream_sse(self):
        class StreamingUpstreamHandler(BaseHTTPRequestHandler):
            seen_bodies = []

            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                StreamingUpstreamHandler.seen_bodies.append(body)
                payload = b'data: {"choices":[{"delta":{"content":"upstream stream"}}]}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), StreamingUpstreamHandler)
            gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            upstream_thread.start()
            gateway_thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
                cfg["upstream"]["model"] = "m"
                cfg["gateway"]["tool_mode"] = "passthrough"
                gateway.save_config(cfg)
                req = urllib.request.Request(
                    f"http://127.0.0.1:{gateway_server.server_address[1]}/v1/chat/completions",
                    data=json.dumps(
                        {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
                    ).encode("utf-8"),
                    headers={"authorization": "Bearer local-gateway-key", "content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
                    text = resp.read().decode("utf-8")
                self.assertIn("upstream stream", text)
                self.assertTrue(StreamingUpstreamHandler.seen_bodies[0]["stream"])
            finally:
                upstream.shutdown()
                gateway_server.shutdown()
                upstream.server_close()
                gateway_server.server_close()
                upstream_thread.join(timeout=2)
                gateway_thread.join(timeout=2)
                gateway._mcp_close_sessions()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_models_and_count_tokens_endpoints_for_claude_code_compatibility(self):
        class ModelUpstreamHandler(BaseHTTPRequestHandler):
            seen_auth = None

            def log_message(self, fmt, *args):
                return

            def do_GET(self):  # noqa: N802
                ModelUpstreamHandler.seen_auth = self.headers.get("authorization")
                payload = json.dumps({"object": "list", "data": [{"id": "mimo-v2.5-pro", "object": "model"}]}).encode("utf-8")
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
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), ModelUpstreamHandler)
            gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            upstream_thread.start()
            gateway_thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
                cfg["upstream"]["api_key"] = "up-key"
                cfg["upstream"]["model"] = "mimo-v2.5-pro"
                gateway.save_config(cfg)
                req = urllib.request.Request(
                    f"http://127.0.0.1:{gateway_server.server_address[1]}/v1/models",
                    headers={"authorization": "Bearer local-gateway-key"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    models = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(models["data"][0]["id"], "mimo-v2.5-pro")
                self.assertEqual(ModelUpstreamHandler.seen_auth, "Bearer up-key")

                count_req = urllib.request.Request(
                    f"http://127.0.0.1:{gateway_server.server_address[1]}/v1/messages/count_tokens",
                    data=json.dumps({"messages": [{"role": "user", "content": "hello world"}]}).encode("utf-8"),
                    headers={"authorization": "Bearer local-gateway-key", "content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(count_req, timeout=5) as resp:
                    count = json.loads(resp.read().decode("utf-8"))
                self.assertGreater(count["input_tokens"], 0)
            finally:
                upstream.shutdown()
                gateway_server.shutdown()
                upstream.server_close()
                gateway_server.server_close()
                upstream_thread.join(timeout=2)
                gateway_thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_messages_use_openai_chat_stream_aggregate_upstream_fast_path(self):
        class ChatStreamHandler(BaseHTTPRequestHandler):
            seen_path = None
            seen_body = None

            def do_POST(self):  # noqa: N802
                ChatStreamHandler.seen_path = self.path
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))).decode("utf-8"))
                ChatStreamHandler.seen_body = body
                payload = (
                    'data: {"id":"chatcmpl_x","model":"m","choices":[{"delta":{"reasoning":"think"}}]}\n\n'
                    'data: {"id":"chatcmpl_x","model":"m","choices":[{"delta":{"content":"hello"}}]}\n\n'
                    'data: {"id":"chatcmpl_x","model":"m","choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'
                    'data: [DONE]\n\n'
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):  # noqa: N802
                pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_force = os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), ChatStreamHandler)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = "1"
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
                cfg["upstream"]["model"] = "m"
                cfg["upstream"]["protocol"] = "openai_chat"
                gateway.save_config(cfg)
                response = gateway.NativeProxyClient().forward(
                    "/v1/messages",
                    {"model": "ignored", "system": "sys", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
                )
                self.assertEqual(ChatStreamHandler.seen_path, "/v1/chat/completions")
                self.assertTrue(ChatStreamHandler.seen_body["stream"])
                self.assertEqual(ChatStreamHandler.seen_body["messages"][0]["role"], "system")
                self.assertEqual(response["content"][-1]["text"], "hello world")
                self.assertEqual(response["content"][0]["type"], "thinking")
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if old_force is None:
                    os.environ.pop("GATEWAY_UPSTREAM_STREAM_AGGREGATE", None)
                else:
                    os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = old_force


    def test_openai_chat_only_upstream_converts_chat_responses_and_messages(self):
        class ChatOnlyHandler(BaseHTTPRequestHandler):
            seen: list[dict] = []

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))).decode("utf-8"))
                ChatOnlyHandler.seen.append({"path": self.path, "body": body})
                payload = json.dumps(
                    {
                        "id": f"chatcmpl_{len(ChatOnlyHandler.seen)}",
                        "object": "chat.completion",
                        "model": body.get("model") or "m",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": f"converted {len(ChatOnlyHandler.seen)}"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):  # noqa: N802
                pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_force = os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), ChatOnlyHandler)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = "0"
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
                cfg["upstream"]["model"] = "m-chat-only"
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["capabilities"]["supports_tools"] = True
                cfg["upstream"]["capabilities"]["supports_function_calls"] = True
                cfg["upstream"]["paths"] = {
                    "models": "/v1/models",
                    "chat_completions": "/v1/chat/completions",
                    "responses": "/upstream-does-not-support-responses",
                    "messages": "/upstream-does-not-support-messages",
                }
                gateway.save_config(cfg)
                client = gateway.NativeProxyClient()

                chat = run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "downstream", "messages": [{"role": "user", "content": "chat request"}]},
                    client,
                )
                responses = run_tool_orchestration(
                    "/v1/responses",
                    {"model": "downstream", "instructions": "be concise", "input": "responses request"},
                    client,
                )
                messages = run_tool_orchestration(
                    "/v1/messages",
                    {"model": "downstream", "max_tokens": 100, "system": "system prompt", "messages": [{"role": "user", "content": [{"type": "text", "text": "messages request"}]}]},
                    client,
                )

                self.assertEqual([item["path"] for item in ChatOnlyHandler.seen], ["/v1/chat/completions"] * 3)
                self.assertEqual(ChatOnlyHandler.seen[0]["body"]["messages"][-1]["content"], "chat request")
                self.assertEqual(ChatOnlyHandler.seen[1]["body"]["messages"][0], {"role": "system", "content": "be concise"})
                self.assertEqual(ChatOnlyHandler.seen[1]["body"]["messages"][1], {"role": "user", "content": "responses request"})
                self.assertEqual(ChatOnlyHandler.seen[2]["body"]["messages"][0], {"role": "system", "content": "system prompt"})
                self.assertEqual(ChatOnlyHandler.seen[2]["body"]["messages"][1], {"role": "user", "content": "messages request"})
                self.assertTrue(all(item["body"]["model"] == "m-chat-only" for item in ChatOnlyHandler.seen))
                self.assertEqual(chat["choices"][0]["message"]["content"], "converted 1")
                self.assertEqual(responses["output"][0]["content"][0]["text"], "converted 2")
                self.assertEqual(messages["content"][-1]["text"], "converted 3")
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if old_force is None:
                    os.environ.pop("GATEWAY_UPSTREAM_STREAM_AGGREGATE", None)
                else:
                    os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = old_force


    def test_conversation_memory_recalls_same_session_workspace_only(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            gateway.SQLITE_READY = False
            try:
                cfg = gateway._default_config()
                cfg["context"]["memory_enabled"] = True
                cfg["context"]["memory_summary_max_chars"] = 500
                cfg["context"]["memory_recall_limit"] = 5
                cfg["upstream"]["tools_enabled"] = "off"
                cfg["gateway"]["local_planner_enabled"] = False
                gateway.save_config(cfg)
                session_meta = {"user_id": json.dumps({"session_id": "session-a"})}
                first_client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "确认：src/toolcall_gateway.py 是启动入口。"}}]}])
                first = run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "m", "metadata": session_meta, "messages": [{"role": "user", "content": "请记住 src/toolcall_gateway.py 是 gateway 启动入口"}]},
                    first_client,
                )
                self.assertIn("启动入口", first["choices"][0]["message"]["content"])
                memories = gateway._sqlite_tail_memories(20)
                self.assertTrue(any("src/toolcall_gateway.py" in item["summary"] for item in memories))

                second_client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "已基于记忆回答"}}]}])
                run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "m", "metadata": session_meta, "messages": [{"role": "user", "content": "src/toolcall_gateway.py 入口是什么？"}]},
                    second_client,
                )
                sent = json.dumps(second_client.requests[0][1], ensure_ascii=False)
                self.assertIn("Gateway recalled memory", sent)
                self.assertIn("src/toolcall_gateway.py", sent)

                other_client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "other"}}]}])
                run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "m", "metadata": {"user_id": json.dumps({"session_id": "session-b"})}, "messages": [{"role": "user", "content": "src/toolcall_gateway.py 入口是什么？"}]},
                    other_client,
                )
                other_sent = json.dumps(other_client.requests[0][1], ensure_ascii=False)
                self.assertNotIn("Gateway recalled memory", other_sent)
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.SQLITE_READY = old_ready
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite

    def test_conversation_memory_compacts_huge_turns_in_sqlite(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            gateway.SQLITE_READY = False
            try:
                cfg = gateway._default_config()
                cfg["context"]["memory_enabled"] = True
                cfg["context"]["memory_summary_max_chars"] = 700
                cfg["gateway"]["local_planner_enabled"] = False
                gateway.save_config(cfg)
                huge = "分析 huge.py " + ("class Huge:\n    pass\n" * 2000)
                client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "Huge 类职责已分析"}}]}])
                run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "m", "metadata": {"session_id": "huge-session"}, "messages": [{"role": "user", "content": huge}]},
                    client,
                )
                memories = gateway._sqlite_tail_memories(5)
                self.assertEqual(len(memories), 1)
                self.assertLessEqual(len(memories[0]["summary"]), 900)
                self.assertIn("gateway context compacted", memories[0]["summary"])
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.SQLITE_READY = old_ready
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite



    def test_more_top_tool_aliases_mcp_memory_and_parallel_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "gateway.sqlite3")
            gateway.SQLITE_READY = False
            try:
                cfg = gateway._default_config()
                gateway.save_config(cfg)
                recipient = execute_direct_tool_call({"recipient_name": "functions.calculator", "parameters": {"expression": "7*6"}})
                self.assertTrue(recipient["success"])
                self.assertEqual(recipient["content"], "42")

                parallel = execute_direct_tool_call(
                    {
                        "tool_uses": [
                            {"recipient_name": "functions.calculator", "parameters": {"expression": "20+22"}},
                            {"recipient_name": "functions.mcp_list_tools", "parameters": {}},
                        ]
                    }
                )
                self.assertTrue(parallel["success"])
                self.assertIn("42", parallel["content"])
                self.assertIn("tools", parallel["content"])

                memory = execute_direct_tool_call({"tool": "SaveMemory", "arguments": {"action": "write", "summary": "top tool aliases verified", "keywords": ["top-tools"]}})
                self.assertTrue(memory["success"])
                recalled = execute_direct_tool_call({"tool": "RecallMemory", "arguments": {"action": "list", "limit": 5}})
                self.assertTrue(recalled["success"])
                self.assertIn("top tool aliases verified", recalled["content"])

                for alias in ["BashOutput", "KillBash", "web_search_preview_2025_03_11", "McpListTools", "McpCallTool", "read_skill", "run_skill"]:
                    self.assertIn(alias, gateway.BUILTIN_TOOLS)
                connector_ready = [
                    name
                    for name, tool in gateway.BUILTIN_TOOLS.items()
                    if name == tool.name and tool.risk == "connector_required"
                ]
                self.assertLessEqual(set(connector_ready), {"click", "type_text", "press_key", "scroll", "computer_use", "computer_use_preview", "computer_call", "image_generation"})
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.SQLITE_READY = old_ready
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite



    def test_client_config_page_generates_codex_opencode_and_claude_snippets(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["upstream"]["model"] = "mimo-v2.5-pro"
                cfg["gateway"]["public_base_url"] = "http://127.0.0.1:8885"
                cfg["gateway"]["client_snippet_api_key"] = "test-api-key"
                gateway.save_config(cfg)
                token = base64.b64encode(b"admin:admin").decode("ascii")
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                req = urllib.request.Request(base + "/client-config.json", headers={"authorization": f"Basic {token}"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertIn('model_provider = "gateway"', payload["codex_config_toml"])
                self.assertIn("[model_providers.gateway]", payload["codex_config_toml"])
                self.assertIn('base_url = "http://127.0.0.1:8885/v1"', payload["codex_config_toml"])
                self.assertIn('env_key = "OPENAI_API_KEY"', payload["codex_config_toml"])
                self.assertIn('wire_api = "responses"', payload["codex_config_toml"])
                self.assertIn("model_context_window = 1048576", payload["codex_config_toml"])
                self.assertIn("model_max_output_tokens = 131072", payload["codex_config_toml"])
                self.assertIn('"OPENAI_API_KEY": "test-api-key"', payload["codex_auth_json"])
                self.assertIn('"baseURL": "http://127.0.0.1:8885/v1"', payload["opencode_json"])
                self.assertIn("claude_mnative()", payload["claude_bash_profile_function"])
                self.assertIn('ANTHROPIC_BASE_URL="http://127.0.0.1:8885/anthropic"', payload["claude_bash_profile_function"])
                self.assertIn('ANTHROPIC_AUTH_TOKEN="test-api-key"', payload["claude_bash_profile_function"])
                self.assertIn('ANTHROPIC_API_KEY=""', payload["claude_bash_profile_function"])
                self.assertIn('command -v claude', payload["claude_bash_profile_function"])
                self.assertIn('"ANTHROPIC_AUTH_TOKEN": "test-api-key"', payload["vscode_claude_settings_json"])
                self.assertEqual(payload["claude_code_env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8885/anthropic")
                self.assertEqual(payload["claude_code_env"]["ANTHROPIC_API_KEY"], "")

                form = urllib.parse.urlencode(
                    {
                        "public_base_url": "http://gateway.local:8885",
                        "client_snippet_api_key": "new-api-key",
                        "downstream_model_alias": "gpt-5.4",
                        "review_model_alias": "gpt-5.4",
                        "codex_reasoning_effort": "xhigh",
                        "client_context_window": "1000000",
                        "client_auto_compact_token_limit": "900000",
                        "client_output_token_limit": "128000",
                    }
                ).encode("utf-8")
                post = urllib.request.Request(
                    base + "/admin/client-config",
                    data=form,
                    headers={"authorization": f"Basic {token}", "content-type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(post, timeout=5).read()
                except Exception as exc:
                    if getattr(exc, "code", None) != 303:
                        raise
                saved = gateway.load_config()
                self.assertEqual(saved["gateway"]["public_base_url"], "http://gateway.local:8885")
                self.assertEqual(saved["gateway"]["downstream_model_alias"], "gpt-5.4")
                self.assertTrue(
                    any(
                        isinstance(item, dict) and item.get("key_hash") == gateway._hash_secret("new-api-key")
                        for item in saved["downstream_keys"]
                    )
                )
                tool_req = urllib.request.Request(
                    base + "/v1/tools/call",
                    data=json.dumps({"tool": "calculator", "arguments": {"expression": "6*7"}}).encode("utf-8"),
                    headers={"authorization": "Bearer new-api-key", "content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(tool_req, timeout=5) as resp:
                    tool_payload = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(tool_payload["success"])
                self.assertEqual(tool_payload["content"], "42")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config



class ThinkTagExtractionTests(unittest.TestCase):
    """Tests for <think> tag extraction and Anthropic thinking block conversion."""

    def test_extract_think_blocks_single(self):
        from src.gateway_protocol import _extract_think_blocks
        text = "<think>reasoning here</think>\nhello"
        think_texts, remaining = _extract_think_blocks(text)
        self.assertEqual(think_texts, ["reasoning here"])
        self.assertEqual(remaining, "hello")

    def test_extract_think_blocks_multiple(self):
        from src.gateway_protocol import _extract_think_blocks
        text = "<think>first</think>\nmid\n<think>second</think>\nend"
        think_texts, remaining = _extract_think_blocks(text)
        self.assertEqual(think_texts, ["first", "second"])
        self.assertEqual(remaining, "mid\n\nend")

    def test_extract_think_blocks_none(self):
        from src.gateway_protocol import _extract_think_blocks
        text = "just a normal response"
        think_texts, remaining = _extract_think_blocks(text)
        self.assertEqual(think_texts, [])
        self.assertEqual(remaining, "just a normal response")

    def test_extract_think_blocks_multiline(self):
        from src.gateway_protocol import _extract_think_blocks
        text = "<think>\nline 1\nline 2\n</think>\nanswer"
        think_texts, remaining = _extract_think_blocks(text)
        self.assertEqual(think_texts, ["line 1\nline 2"])
        self.assertEqual(remaining, "answer")

    def test_openai_response_converts_think_to_thinking_blocks(self):
        from src.gateway_protocol import _from_openai_chat_response
        response = {
            "id": "msg_test",
            "model": "mimo-v2.5-pro",
            "choices": [{
                "message": {
                    "content": "<think>user said hi, I should respond politely</think>\nhi! 有什么我可以帮你的吗？"
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        result = _from_openai_chat_response("/v1/messages", response)
        content = result["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["type"], "thinking")
        self.assertEqual(content[0]["thinking"], "user said hi, I should respond politely")
        self.assertEqual(content[1]["type"], "text")
        self.assertEqual(content[1]["text"], "hi! 有什么我可以帮你的吗？")

    def test_openai_response_no_think_tags(self):
        from src.gateway_protocol import _from_openai_chat_response
        response = {
            "id": "msg_test",
            "model": "mimo-v2.5-pro",
            "choices": [{
                "message": {"content": "plain response"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = _from_openai_chat_response("/v1/messages", response)
        content = result["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "plain response")

    def test_openai_response_think_only_no_text(self):
        from src.gateway_protocol import _from_openai_chat_response
        response = {
            "id": "msg_test",
            "model": "mimo-v2.5-pro",
            "choices": [{
                "message": {"content": "<think>just thinking, no answer</think>"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = _from_openai_chat_response("/v1/messages", response)
        content = result["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "thinking")

    def test_openai_response_reasoning_field_still_works(self):
        from src.gateway_protocol import _from_openai_chat_response
        response = {
            "id": "msg_test",
            "model": "mimo-v2.5-pro",
            "choices": [{
                "message": {"reasoning": "structured reasoning", "content": "answer"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = _from_openai_chat_response("/v1/messages", response)
        content = result["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["type"], "thinking")
        self.assertEqual(content[0]["thinking"], "structured reasoning")
        self.assertEqual(content[1]["type"], "text")


class LocalPlannerTriggerTests(unittest.TestCase):
    """Tests for local planner trigger keyword tightening."""

    def test_common_greeting_does_not_trigger(self):
        from src.gateway_tool_runtime import _should_build_local_planner_context
        body = {"messages": [{"role": "user", "content": "你好"}]}
        self.assertFalse(_should_build_local_planner_context("/v1/messages", body))

    def test_simple_question_does_not_trigger(self):
        from src.gateway_tool_runtime import _should_build_local_planner_context
        body = {"messages": [{"role": "user", "content": "今天天气怎么样？"}]}
        self.assertFalse(_should_build_local_planner_context("/v1/messages", body))

    def test_code_word_alone_does_not_trigger(self):
        from src.gateway_tool_runtime import _should_build_local_planner_context
        body = {"messages": [{"role": "user", "content": "帮我写一段代码"}]}
        self.assertFalse(_should_build_local_planner_context("/v1/messages", body))

    def test_analyze_alone_does_not_trigger(self):
        from src.gateway_tool_runtime import _should_build_local_planner_context
        body = {"messages": [{"role": "user", "content": "分析一下这个问题"}]}
        self.assertFalse(_should_build_local_planner_context("/v1/messages", body))

    def test_at_sign_alone_does_not_trigger(self):
        from src.gateway_tool_runtime import _should_build_local_planner_context
        body = {"messages": [{"role": "user", "content": "email me@test.com"}]}
        self.assertFalse(_should_build_local_planner_context("/v1/messages", body))

    def test_analyze_code_with_path_triggers(self):
        from src.gateway_tool_runtime import _should_build_local_planner_context
        body = {"messages": [{"role": "user", "content": "分析代码 src/main.py"}]}
        self.assertTrue(_should_build_local_planner_context("/v1/messages", body))

    def test_read_file_triggers(self):
        from src.gateway_tool_runtime import _should_build_local_planner_context
        body = {"messages": [{"role": "user", "content": "读取 src/config.py"}]}
        self.assertTrue(_should_build_local_planner_context("/v1/messages", body))


class AnthropicSSEFormatTests(unittest.TestCase):
    """Tests for complete Anthropic SSE format in streaming responses."""

    def test_stream_final_response_has_message_start(self):
        from src.gateway_streaming import _stream_final_response
        events = []
        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)
                @staticmethod
                def flush():
                    pass
        response = {
            "id": "msg_test",
            "model": "mimo-v2.5-pro",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        _stream_final_response(FakeHandler(), "/v1/messages", response)
        all_text = "".join(events)
        self.assertIn('"message_start"', all_text)
        self.assertIn('"message_delta"', all_text)
        self.assertIn('"message_stop"', all_text)
        self.assertIn('"stop_reason"', all_text)


    def test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream(self):
        from src.gateway_streaming import run_streaming_orchestration

        events = []

        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)

                @staticmethod
                def flush():
                    pass

            def send_response(self, status):
                events.append(f"STATUS:{status}\n")

            def send_header(self, key, value):
                events.append(f"HEADER:{key}:{value}\n")

            def end_headers(self):
                events.append("END_HEADERS\n")

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_mode = os.environ.get("GATEWAY_TOOL_MODE")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                workspace = pathlib.Path(td) / "workspace"
                workspace.mkdir()
                probe = workspace / "probe.txt"
                probe.write_text("gateway-local-file-probe: 2+2=4\n", encoding="utf-8")
                os.environ.pop("GATEWAY_TOOL_MODE", None)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "auto"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                run_streaming_orchestration(
                    FakeHandler(),
                    "/v1/messages",
                    {
                        "model": "m",
                        "stream": True,
                        "messages": [
                            {
                                "role": "user",
                                "content": f"Read local file {probe} and answer only the value after gateway-local-file-probe.",
                            }
                        ],
                        "max_tokens": 64,
                    },
                )

                text = "".join(events)
                self.assertIn("text/event-stream", text)
                self.assertIn('"type": "tool_use"', text)
                self.assertIn('"name": "Read"', text)
                self.assertIn(str(probe), text)
                self.assertIn('"stop_reason": "tool_use"', text)
                self.assertNotIn("event: error", text)
                self.assertIn("message_stop", text)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_mode is None:
                    os.environ.pop("GATEWAY_TOOL_MODE", None)
                else:
                    os.environ["GATEWAY_TOOL_MODE"] = old_mode

    def test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool(self):
        from src.gateway_streaming import run_streaming_orchestration

        events = []

        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)

                @staticmethod
                def flush():
                    pass

            def send_response(self, status):
                events.append(f"STATUS:{status}\n")

            def send_header(self, key, value):
                events.append(f"HEADER:{key}:{value}\n")

            def end_headers(self):
                events.append("END_HEADERS\n")

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_mode = os.environ.get("GATEWAY_TOOL_MODE")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                os.environ.pop("GATEWAY_TOOL_MODE", None)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = td
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["gateway"]["allow_shell_tools"] = True
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                run_streaming_orchestration(
                    FakeHandler(),
                    "/v1/messages",
                    {
                        "model": "m",
                        "stream": True,
                        "messages": [
                            {
                                "role": "user",
                                "content": "Run bash command `printf STREAM-BASH-OK` and reply only with stdout.",
                            }
                        ],
                        "max_tokens": 64,
                    },
                )

                text = "".join(events)
                self.assertIn('"type": "tool_use"', text)
                self.assertIn('"name": "Bash"', text)
                self.assertIn("printf STREAM-BASH-OK", text)
                self.assertIn('"stop_reason": "tool_use"', text)
                self.assertNotIn("event: error", text)
                self.assertIn("message_stop", text)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_mode is None:
                    os.environ.pop("GATEWAY_TOOL_MODE", None)
                else:
                    os.environ["GATEWAY_TOOL_MODE"] = old_mode

    def test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool(self):
        from src.gateway_streaming import run_streaming_orchestration

        events = []

        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)

                @staticmethod
                def flush():
                    pass

            def send_response(self, status):
                events.append(f"STATUS:{status}\n")

            def send_header(self, key, value):
                events.append(f"HEADER:{key}:{value}\n")

            def end_headers(self):
                events.append("END_HEADERS\n")

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_mode = os.environ.get("GATEWAY_TOOL_MODE")
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                workspace = pathlib.Path(td) / "workspace"
                skill_file = workspace / ".codex/skills/deep-think/SKILL.md"
                skill_file.parent.mkdir(parents=True, exist_ok=True)
                skill_file.write_text("# deep-think\n\nmarker: STREAM-SKILL-OK\n", encoding="utf-8")
                os.environ["GATEWAY_WORKSPACE_ROOT"] = str(workspace)
                os.environ.pop("GATEWAY_TOOL_MODE", None)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                run_streaming_orchestration(
                    FakeHandler(),
                    "/v1/messages",
                    {
                        "model": "m",
                        "stream": True,
                        "messages": [{"role": "user", "content": "Read skill deep-think and return its content."}],
                        "max_tokens": 128,
                    },
                )

                text = "".join(events)
                self.assertIn('"type": "tool_use"', text)
                self.assertIn('"name": "Skill"', text)
                self.assertIn("deep-think", text)
                self.assertIn('"stop_reason": "tool_use"', text)
                self.assertNotIn("event: error", text)
                self.assertIn("message_stop", text)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_mode is None:
                    os.environ.pop("GATEWAY_TOOL_MODE", None)
                else:
                    os.environ["GATEWAY_TOOL_MODE"] = old_mode
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_stream_final_response_emits_thinking_blocks(self):
        from src.gateway_streaming import _stream_final_response
        events = []
        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)
                @staticmethod
                def flush():
                    pass
        response = {
            "id": "msg_test",
            "model": "mimo-v2.5-pro",
            "content": [
                {"type": "thinking", "thinking": "reasoning here"},
                {"type": "text", "text": "answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        _stream_final_response(FakeHandler(), "/v1/messages", response)
        all_text = "".join(events)
        self.assertIn('"thinking_delta"', all_text)
        self.assertIn("reasoning here", all_text)
        self.assertIn('"text_delta"', all_text)

    def test_stream_final_response_responses_has_item_before_text_delta(self):
        from src.gateway_streaming import _stream_final_response
        events = []

        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)

                @staticmethod
                def flush():
                    pass

        response = {
            "id": "resp_test",
            "object": "response",
            "model": "mimo-v2.5-pro",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "CODEX-STREAM-OK"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

        _stream_final_response(FakeHandler(), "/v1/responses", response)
        all_text = "".join(events)

        self.assertIn("event: response.output_item.added", all_text)
        self.assertIn("event: response.content_part.added", all_text)
        self.assertIn("event: response.output_text.delta", all_text)
        self.assertIn("event: response.output_text.done", all_text)
        self.assertIn("event: response.completed", all_text)
        self.assertLess(all_text.index("response.output_item.added"), all_text.index("response.output_text.delta"))
        self.assertIn("CODEX-STREAM-OK", all_text)


if __name__ == "__main__":
    unittest.main()


# ─────────────────────────────────────────────────────────────────
# Streaming / SSE Tests
# ─────────────────────────────────────────────────────────────────

class StreamingToolEventTests(unittest.TestCase):
    """Tests for P1 streaming tool event implementation."""

    def test_parse_sse_line_data(self):
        event, data = gateway._parse_sse_line("data: {\"foo\": 1}")
        self.assertIsNone(event)
        self.assertEqual(data, '{"foo": 1}')

    def test_parse_sse_line_event_and_data(self):
        event, data = gateway._parse_sse_line('event: tool_use\ndata: {"type":"tool_use"}')
        self.assertEqual(event, "tool_use")
        self.assertEqual(data, '{"type":"tool_use"}')

    def test_parse_sse_line_done(self):
        event, data = gateway._parse_sse_line("data: [DONE]")
        self.assertIsNone(event)
        self.assertEqual(data, "[DONE]")

    def test_parse_sse_line_comment(self):
        event, data = gateway._parse_sse_line(": comment")
        self.assertIsNone(event)
        self.assertIsNone(data)

    def test_parse_sse_line_empty(self):
        event, data = gateway._parse_sse_line("")
        self.assertIsNone(event)
        self.assertIsNone(data)

    def test_parse_sse_line_real_openai_chunk(self):
        # Real SSE line format from OpenAI streaming
        line = 'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_abc","type":"function","function":{"name":"echo_probe","arguments":"{}"}}]}}]})'
        event, data = gateway._parse_sse_line(line)
        self.assertIsNone(event)
        parsed = json.loads(data)
        self.assertEqual(parsed["choices"][0]["delta"]["tool_calls"][0]["function"]["name"], "echo_probe")

    def test_parse_sse_line_real_anthropic_event(self):
        line = 'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"echo_probe","input":{}}}'
        event, data = gateway._parse_sse_line(line)
        self.assertEqual(event, "content_block_start")
        parsed = json.loads(data)
        self.assertEqual(parsed["content_block"]["name"], "echo_probe")

    # ── _detect_streaming_tool_calls_from_sse ──────────────────────

    def test_detect_openai_delta(self):
        """OpenAI SSE delta fragment → call_id + name + partial args."""
        line = 'data: {"id":"c","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"echo_probe","arguments":"{}"}}]}}]})'
        _, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/chat/completions", None, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "call_1")
        self.assertEqual(calls[0]["name"], "echo_probe")
        self.assertEqual(calls[0]["arguments"], "{}")

    def test_detect_openai_arguments_fragment(self):
        """OpenAI arguments arrive in a separate delta chunk."""
        # First chunk: just function name
        line1 = 'data: {"id":"c","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"echo_probe"}}]}}]})'
        _, d1 = gateway._parse_sse_line(line1)
        _, d1 = gateway._parse_sse_line(line1)
        calls1 = gateway._detect_streaming_tool_calls_from_sse("/v1/chat/completions", None, d1)
        self.assertEqual(calls1[0]["name"], "echo_probe")
        self.assertEqual(calls1[0].get("arguments", ""), "")

        # Second chunk: partial arguments (complete JSON string fragment)
        line2 = 'data: {"id":"c","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"value\\": 1}"}}]}}]})'
        _, d2 = gateway._parse_sse_line(line2)
        calls2 = gateway._detect_streaming_tool_calls_from_sse("/v1/chat/completions", None, d2)
        self.assertEqual(len(calls2), 1)
        self.assertEqual(calls2[0]["arguments"], '{"value": 1}')

    def test_detect_openai_done(self):
        """OpenAI [DONE] sentinel → empty list."""
        _, data = gateway._parse_sse_line("data: [DONE]")
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/chat/completions", None, data)
        self.assertEqual(calls, [])

    def test_detect_openai_text_delta_ignored(self):
        """OpenAI text content delta → not a tool call."""
        line = 'data: {"id":"c","choices":[{"delta":{"content":"The result"}}]})'
        _, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/chat/completions", None, data)
        self.assertEqual(calls, [])

    def test_detect_anthropic_content_block_start(self):
        """Anthropic content_block_start with type=tool_use → parsed."""
        line = 'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"echo_probe","input":{}}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/messages", event, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "toolu_1")
        self.assertEqual(calls[0]["name"], "echo_probe")

    def test_detect_anthropic_content_block_delta(self):
        """Anthropic content_block_delta with input_json_delta → partial result."""
        line = 'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"value\\":"}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/messages", event, data)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("_partial"))
        self.assertEqual(calls[0]["arguments"], '{"value":')
        self.assertEqual(calls[0]["_index"], 0)

    def test_detect_anthropic_text_delta_ignored(self):
        """Anthropic text content_block → not a tool call."""
        line = 'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/messages", event, data)
        self.assertEqual(calls, [])

    def test_detect_anthropic_content_block_stop(self):
        """Anthropic content_block_stop → block_stop signal."""
        line = 'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/messages", event, data)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("_block_stop"))
        self.assertEqual(calls[0]["_index"], 0)

    def test_detect_anthropic_text_delta_non_tool(self):
        """Anthropic content_block_delta for text → ignored."""
        line = 'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/messages", event, data)
        self.assertEqual(calls, [])

    def test_detect_responses_function_call(self):
        """OpenAI Responses output_item event with 'output' field → parsed."""
        line = 'event: response.output_item.done\ndata: {"type":"response.output_item.done","output":{"type":"function_call","call_id":"rc_1","name":"echo_probe","arguments":"{\\"value\\":\\"probe\\"}"}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/responses", event, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "rc_1")
        self.assertEqual(calls[0]["name"], "echo_probe")
        self.assertEqual(calls[0]["arguments"], '{"value":"probe"}')

    def test_detect_responses_function_call_item_field(self):
        """Responses output_item.done with 'item' field (fufu format) → parsed."""
        line = 'event: response.output_item.done\ndata: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_abc","call_id":"call_xyz","name":"calc","arguments":"{\\"expr\\": \\"2+2\\"}"}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/responses", event, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "call_xyz")
        self.assertEqual(calls[0]["name"], "calc")
        self.assertEqual(calls[0]["arguments"], '{"expr": "2+2"}')

    def test_detect_responses_function_call_arguments_done(self):
        """Responses function_call_arguments.done → parsed."""
        line = 'event: response.function_call_arguments.done\ndata: {"type":"response.function_call_arguments.done","output_index":0,"item":{"type":"function_call","id":"fc_abc","call_id":"call_xyz","name":"calc","arguments":"{\\"expr\\": \\"2+2\\"}"}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/responses", event, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "call_xyz")
        self.assertEqual(calls[0]["name"], "calc")

    def test_detect_responses_output_item_added(self):
        """Responses output_item.added with function_call → initial signal."""
        line = 'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_abc","call_id":"call_xyz","name":"calc","arguments":""}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/responses", event, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "call_xyz")
        self.assertEqual(calls[0]["name"], "calc")
        self.assertTrue(calls[0].get("_initial"))

    def test_detect_responses_partial_args(self):
        """Responses function_call_arguments.delta → partial result."""
        line = 'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\"expr\\": \\"2+2\\"}"}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/responses", event, data)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("_partial"))
        self.assertEqual(calls[0]["arguments"], '{"expr": "2+2"}')

    def test_detect_unknown_event_passthrough(self):
        """Unknown event name → still try to parse as JSON data."""
        line = 'event: custom_event\ndata: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"x"}}]}}]})'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/chat/completions", event, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "x")

    def test_detect_invalid_json_returns_empty(self):
        """Non-JSON data line → empty list, no crash."""
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/chat/completions", None, "not json at all")
        self.assertEqual(calls, [])

    # ── _streaming_tool_event_for_path ─────────────────────────────

    def test_streaming_tool_event_openai(self):
        result = gateway.ToolResult(
            call_id="call_1", name="echo_probe",
            content=json.dumps({"value": "ok"}),
            success=True,
        )
        events = gateway._streaming_tool_event_for_path(
            "/v1/chat/completions", "call_1", "echo_probe",
            {"value": "ok"}, result, "chatcmpl-1", 0,
        )
        self.assertIsInstance(events, list)
        self.assertEqual(len(events), 2)
        # First event: delta with tool_calls
        event_name, payload = events[0]
        self.assertEqual(event_name, "chatcmpl")
        tc = payload["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tc["id"], "call_1")
        self.assertEqual(tc["function"]["name"], "echo_probe")
        self.assertIsNone(payload["choices"][0]["finish_reason"])
        # Second event: finish_reason=tool_calls
        event_name2, payload2 = events[1]
        self.assertEqual(event_name2, "chatcmpl")
        self.assertEqual(payload2["choices"][0]["finish_reason"], "tool_calls")

    def test_streaming_tool_event_anthropic(self):
        result = gateway.ToolResult(
            call_id="toolu_1", name="echo_probe",
            content=json.dumps({"value": "ok"}),
            success=True,
        )
        events = gateway._streaming_tool_event_for_path(
            "/v1/messages", "toolu_1", "echo_probe",
            {"value": "ok"}, result, "msg_1", 0,
        )
        # Should produce: content_block_start → content_block_delta → content_block_stop
        self.assertEqual(len(events), 3)
        event_names = [e[0] for e in events]
        self.assertEqual(event_names, ["content_block_start", "content_block_delta", "content_block_stop"])
        # Verify content_block_start
        _, start_payload = events[0]
        self.assertEqual(start_payload["type"], "content_block_start")
        cb = start_payload["content_block"]
        self.assertEqual(cb["type"], "tool_use")
        self.assertEqual(cb["id"], "toolu_1")
        self.assertEqual(cb["name"], "echo_probe")
        # Verify content_block_delta
        _, delta_payload = events[1]
        self.assertEqual(delta_payload["type"], "content_block_delta")
        self.assertEqual(delta_payload["delta"]["type"], "input_json_delta")
        self.assertIn("partial_json", delta_payload["delta"])
        # Verify content_block_stop
        _, stop_payload = events[2]
        self.assertEqual(stop_payload["type"], "content_block_stop")

    def test_streaming_tool_event_responses(self):
        result = gateway.ToolResult(
            call_id="rc_1", name="echo_probe",
            content=json.dumps({"value": "ok"}),
            success=True,
        )
        events = gateway._streaming_tool_event_for_path(
            "/v1/responses", "rc_1", "echo_probe",
            {"value": "ok"}, result, "resp_1", 0,
        )
        self.assertGreater(len(events), 0)
        # For responses, check the item type in each event
        item_types = {payload.get("item", {}).get("type") for _, payload in events}
        self.assertIn("function_call", item_types)

    def test_streaming_tool_event_error_openai(self):
        result = gateway.ToolResult(
            call_id="call_1", name="echo_probe",
            content="tool failed",
            success=False,
            failure_type="execution_error",
        )
        events = gateway._streaming_tool_event_for_path(
            "/v1/chat/completions", "call_1", "echo_probe",
            {}, result, "chatcmpl-1", 0,
        )
        self.assertGreater(len(events), 0)
        # For chat.completions, check finish_reason in choices or tool_calls in delta
        # events is list of (event_name, payload) tuples
        has_tool_calls = any(
            payload.get("choices", [{}])[0].get("finish_reason") == "tool_calls"
            or payload.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
            for _, payload in events
        )
        self.assertTrue(has_tool_calls)

    # ── _forced_tool_name ──────────────────────────────────────────

    def test_forced_tool_name_openai(self):
        body = {"tool_choice": {"type": "function", "function": {"name": "calculator"}}}
        self.assertEqual(gateway._forced_tool_name("/v1/chat/completions", body), "calculator")

    def test_forced_tool_name_anthropic(self):
        body = {"tool_choice": {"type": "tool", "name": "calculator"}}
        self.assertEqual(gateway._forced_tool_name("/v1/messages", body), "calculator")

    def test_forced_tool_name_responses(self):
        body = {"tool_choice": {"type": "function", "name": "calculator"}}
        self.assertEqual(gateway._forced_tool_name("/v1/responses", body), "calculator")

    def test_forced_tool_name_auto_is_not_forced(self):
        body = {"tool_choice": "auto"}
        self.assertEqual(gateway._forced_tool_name("/v1/chat/completions", body), "")

    def test_forced_tool_name_missing(self):
        body = {"messages": []}
        self.assertEqual(gateway._forced_tool_name("/v1/chat/completions", body), "")

    # ── _stream_mode_passthrough ───────────────────────────────────

    def test_stream_mode_passthrough_env_off(self):
        old = os.environ.get("GATEWAY_TOOL_MODE", "")
        try:
            os.environ["GATEWAY_TOOL_MODE"] = "0"
            self.assertFalse(gateway._stream_mode_passthrough())
        finally:
            if old:
                os.environ["GATEWAY_TOOL_MODE"] = old
            else:
                os.environ.pop("GATEWAY_TOOL_MODE", None)

    def test_stream_mode_passthrough_env_passthrough(self):
        old = os.environ.get("GATEWAY_TOOL_MODE", "")
        try:
            os.environ["GATEWAY_TOOL_MODE"] = "passthrough"
            self.assertTrue(gateway._stream_mode_passthrough())
        finally:
            if old:
                os.environ["GATEWAY_TOOL_MODE"] = old
            else:
                os.environ.pop("GATEWAY_TOOL_MODE", None)

    def test_stream_mode_passthrough_default_orchestrate(self):
        old = os.environ.get("GATEWAY_TOOL_MODE", "")
        try:
            os.environ.pop("GATEWAY_TOOL_MODE", None)
            self.assertFalse(gateway._stream_mode_passthrough())
        finally:
            if old:
                os.environ["GATEWAY_TOOL_MODE"] = old
            elif "GATEWAY_TOOL_MODE" in os.environ:
                del os.environ["GATEWAY_TOOL_MODE"]

    # ── run_streaming_orchestration signature smoke ─────────────────

    def test_streaming_orchestration_needs_handler_path_body(self):
        """Smoke: run_streaming_orchestration exists and has the right signature."""
        import inspect
        sig = inspect.signature(gateway.run_streaming_orchestration)
        params = list(sig.parameters.keys())
        self.assertIn("handler", params)
        self.assertIn("path", params)
        self.assertIn("body", params)


class ProtocolConversionTests(unittest.TestCase):
    """Tests for Anthropic ↔ OpenAI protocol conversion functions."""

    # ── _anthropic_tools_to_openai ───────────────────────────────

    def test_anthropic_tools_to_openai_basic(self):
        from src.gateway_app import _anthropic_tools_to_openai
        tools = [
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
        result = _anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["function"]["name"], "read_file")
        self.assertEqual(result[0]["function"]["description"], "Read a file")
        self.assertEqual(result[0]["function"]["parameters"]["type"], "object")
        self.assertIn("path", result[0]["function"]["parameters"]["properties"])

    def test_anthropic_tools_to_openai_empty(self):
        from src.gateway_app import _anthropic_tools_to_openai
        self.assertEqual(_anthropic_tools_to_openai([]), [])

    def test_anthropic_tools_to_openai_skips_no_name(self):
        from src.gateway_app import _anthropic_tools_to_openai
        tools = [{"description": "no name"}, {"name": "ok", "input_schema": {}}]
        result = _anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["function"]["name"], "ok")

    def test_anthropic_tools_to_openai_fallback_parameters(self):
        """If input_schema is missing, falls back to 'parameters' key."""
        from src.gateway_app import _anthropic_tools_to_openai
        tools = [{"name": "tool1", "parameters": {"type": "object"}}]
        result = _anthropic_tools_to_openai(tools)
        self.assertEqual(result[0]["function"]["parameters"], {"type": "object"})

    # ── _openai_tools_to_anthropic ───────────────────────────────

    def test_openai_tools_to_anthropic_basic(self):
        from src.gateway_app import _openai_tools_to_anthropic
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
        result = _openai_tools_to_anthropic(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "read_file")
        self.assertEqual(result[0]["description"], "Read a file")
        self.assertIn("input_schema", result[0])
        self.assertEqual(result[0]["input_schema"]["type"], "object")

    def test_openai_tools_to_anthropic_roundtrip(self):
        """Anthropic → OpenAI → Anthropic should preserve name and schema."""
        from src.gateway_app import _anthropic_tools_to_openai, _openai_tools_to_anthropic
        original = [
            {
                "name": "bash",
                "description": "Run a command",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ]
        openai = _anthropic_tools_to_openai(original)
        back = _openai_tools_to_anthropic(openai)
        self.assertEqual(back[0]["name"], "bash")
        self.assertEqual(back[0]["description"], "Run a command")
        self.assertEqual(back[0]["input_schema"], original[0]["input_schema"])

    # ── _anthropic_tool_choice_to_openai ─────────────────────────

    def test_tool_choice_auto(self):
        from src.gateway_app import _anthropic_tool_choice_to_openai
        self.assertEqual(_anthropic_tool_choice_to_openai({"type": "auto"}), "auto")

    def test_tool_choice_any(self):
        from src.gateway_app import _anthropic_tool_choice_to_openai
        self.assertEqual(_anthropic_tool_choice_to_openai({"type": "any"}), "required")

    def test_tool_choice_specific(self):
        from src.gateway_app import _anthropic_tool_choice_to_openai
        result = _anthropic_tool_choice_to_openai({"type": "tool", "name": "bash"})
        self.assertEqual(result, {"type": "function", "function": {"name": "bash"}})

    def test_tool_choice_passthrough_non_dict(self):
        from src.gateway_app import _anthropic_tool_choice_to_openai
        self.assertEqual(_anthropic_tool_choice_to_openai("auto"), "auto")
        self.assertIsNone(_anthropic_tool_choice_to_openai(None))

    def test_tool_choice_tool_missing_name(self):
        from src.gateway_app import _anthropic_tool_choice_to_openai
        # type "tool" without name falls back to "auto"
        self.assertEqual(_anthropic_tool_choice_to_openai({"type": "tool"}), "auto")

    # ── _convert_anthropic_messages_to_openai ────────────────────

    def test_convert_simple_text_messages(self):
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result, system, reasoning = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], "Hello")
        self.assertEqual(result[1]["role"], "assistant")
        self.assertEqual(result[1]["content"], "Hi there")
        self.assertIsNone(reasoning)

    def test_convert_tool_use_to_tool_calls(self):
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me read that file."},
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "read_file",
                        "input": {"path": "/tmp/test.txt"},
                    },
                ],
            }
        ]
        result, system, reasoning = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(len(result), 1)
        msg = result[0]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "Let me read that file.")
        self.assertIn("tool_calls", msg)
        self.assertEqual(len(msg["tool_calls"]), 1)
        tc = msg["tool_calls"][0]
        self.assertEqual(tc["id"], "toolu_123")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "read_file")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"path": "/tmp/test.txt"})

    def test_convert_tool_result_to_role_tool(self):
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_123",
                        "content": "file contents here",
                    }
                ],
            }
        ]
        result, system, reasoning = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "tool")
        self.assertEqual(result[0]["tool_call_id"], "toolu_123")
        self.assertEqual(result[0]["content"], "file contents here")

    def test_convert_tool_result_with_list_content(self):
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_456",
                        "content": [
                            {"type": "text", "text": "line 1"},
                            {"type": "text", "text": "line 2"},
                        ],
                    }
                ],
            }
        ]
        result, _, _ = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(len(result), 1)
        self.assertIn("line 1", result[0]["content"])
        self.assertIn("line 2", result[0]["content"])

    def test_convert_thinking_block(self):
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me reason about this..."},
                    {"type": "text", "text": "The answer is 42."},
                ],
            }
        ]
        result, system, reasoning = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "The answer is 42.")
        self.assertEqual(reasoning, "Let me reason about this...")

    def test_convert_multiple_tool_calls_in_one_message(self):
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll do both."},
                    {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a"}},
                    {"type": "tool_use", "id": "t2", "name": "write", "input": {"path": "b", "content": "x"}},
                ],
            }
        ]
        result, _, _ = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(len(result[0]["tool_calls"]), 2)
        self.assertEqual(result[0]["tool_calls"][0]["function"]["name"], "read")
        self.assertEqual(result[0]["tool_calls"][1]["function"]["name"], "write")

    def test_convert_user_text_and_tool_result_mixed(self):
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Here's the result:"},
                    {"type": "tool_result", "tool_use_id": "t1", "content": "data"},
                ],
            }
        ]
        result, _, _ = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], "Here's the result:")
        self.assertEqual(result[1]["role"], "tool")
        self.assertEqual(result[1]["tool_call_id"], "t1")

    def test_convert_skips_non_dict_messages(self):
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [None, "not a dict", {"role": "user", "content": "ok"}]
        result, _, _ = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "ok")

    # ── _preserve_anthropic_fields ───────────────────────────────

    def test_preserve_anthropic_fields(self):
        from src.gateway_app import _preserve_anthropic_fields
        body = {
            "model": "claude-sonnet-4-20250514",
            "thinking": {"type": "enabled", "budget_tokens": 10000},
            "context_management": {"type": "auto"},
            "stream": True,
        }
        payload: dict = {}
        _preserve_anthropic_fields(body, payload)
        ctx = payload["gateway_context"]
        self.assertEqual(ctx["anthropic_thinking"]["type"], "enabled")
        self.assertEqual(ctx["anthropic_context_management"]["type"], "auto")
        self.assertNotIn("anthropic_output_config", ctx)
        self.assertNotIn("anthropic_metadata", ctx)

    def test_preserve_anthropic_fields_none_values(self):
        from src.gateway_app import _preserve_anthropic_fields
        body = {"model": "test"}
        payload: dict = {}
        _preserve_anthropic_fields(body, payload)
        # gateway_context should be created but empty
        self.assertEqual(payload["gateway_context"], {})

    # ── _to_openai_chat_payload integration ──────────────────────

    def test_to_openai_chat_converts_tools(self):
        from src.gateway_app import _to_openai_chat_payload
        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "bash",
                    "description": "Run command",
                    "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                }
            ],
            "tool_choice": {"type": "auto"},
        }
        payload = _to_openai_chat_payload("/v1/messages", body)
        # Tools should be converted, not stripped
        self.assertIn("tools", payload)
        self.assertEqual(len(payload["tools"]), 1)
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["function"]["name"], "bash")
        # tool_choice should be converted
        self.assertEqual(payload["tool_choice"], "auto")

    def test_to_openai_chat_preserves_tool_chain(self):
        from src.gateway_app import _to_openai_chat_payload
        body = {
            "model": "test",
            "messages": [
                {"role": "user", "content": "read file"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "ok"},
                        {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "/f"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "file data"},
                    ],
                },
            ],
            "stream": True,
        }
        payload = _to_openai_chat_payload("/v1/messages", body)
        msgs = payload["messages"]
        # Should have: user, assistant (with tool_calls), tool
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertIn("tool_calls", msgs[1])
        self.assertEqual(msgs[1]["tool_calls"][0]["function"]["name"], "read")
        self.assertEqual(msgs[2]["role"], "tool")
        self.assertEqual(msgs[2]["tool_call_id"], "t1")
        self.assertEqual(msgs[2]["content"], "file data")

    def test_to_openai_chat_preserves_anthropic_fields(self):
        from src.gateway_app import _to_openai_chat_payload
        body = {
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 5000},
            "context_management": {"type": "auto"},
            "stream": True,
        }
        payload = _to_openai_chat_payload("/v1/messages", body)
        ctx = payload.get("gateway_context", {})
        self.assertIn("anthropic_thinking", ctx)
        self.assertEqual(ctx["anthropic_thinking"]["budget_tokens"], 5000)
        self.assertIn("anthropic_context_management", ctx)
        # These should NOT be in the payload root
        self.assertNotIn("thinking", payload)
        self.assertNotIn("context_management", payload)

    def test_round_trip_anthropic_to_openai_chat(self):
        """Anthropic request -> OpenAI Chat payload preserves message content."""
        from src.gateway_app import _to_openai_chat_payload
        body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "system": "You are helpful.",
            "messages": [
                {"role": "user", "content": "What is 2+2?"},
                {"role": "assistant", "content": "4"},
                {"role": "user", "content": "Why?"},
            ],
        }
        payload = _to_openai_chat_payload("/v1/messages", body)
        msgs = payload["messages"]
        # System message should be first
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("helpful", msgs[0]["content"])
        # User messages preserved
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "What is 2+2?")
        self.assertEqual(msgs[2]["role"], "assistant")
        self.assertEqual(msgs[2]["content"], "4")
        self.assertEqual(msgs[3]["role"], "user")
        self.assertEqual(msgs[3]["content"], "Why?")

    def test_round_trip_thinking_not_leaked_to_system(self):
        """Thinking blocks should NOT become system prompt."""
        from src.gateway_app import _convert_anthropic_messages_to_openai
        messages = [
            {"role": "system", "content": "Real system prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "Hi!"},
            ]},
        ]
        result, system_text, reasoning = _convert_anthropic_messages_to_openai(messages)
        # System prompt should be preserved, not replaced by thinking
        self.assertEqual(system_text, "Real system prompt")
        # Reasoning should be separate
        self.assertEqual(reasoning, "Let me think...")
        # Assistant message should have text content
        assistant = [m for m in result if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["content"], "Hi!")

    def test_convert_response_cross_protocol_anthropic_to_responses(self):
        """Anthropic upstream response -> Responses downstream format."""
        from src.gateway_protocol import _convert_response_to_downstream
        anthropic_response = {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-20250514",
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = _convert_response_to_downstream("/v1/responses", anthropic_response, "anthropic_messages")
        # Should be in Responses format, not Chat format
        self.assertIn("output", result)
        self.assertNotIn("choices", result)

    def test_convert_response_cross_protocol_responses_to_anthropic(self):
        """Responses upstream response -> Anthropic downstream format."""
        from src.gateway_protocol import _convert_response_to_downstream
        responses_response = {
            "id": "resp_123",
            "object": "response",
            "model": "gpt-4",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hi there!"}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = _convert_response_to_downstream("/v1/messages", responses_response, "openai_responses")
        # Should be in Anthropic format
        self.assertIn("content", result)
        self.assertNotIn("choices", result)
        # Content should be a list of blocks
        self.assertIsInstance(result["content"], list)


class ContextSummarizationTests(unittest.TestCase):
    """Tests for LLM-based context summarization."""

    def test_compact_messages_with_summary_fallback(self):
        """When LLM is unavailable, falls back to text trimming."""
        from src.gateway_app import _compact_messages_with_summary
        messages = [
            {"role": "user", "content": "Hello " * 1000},
            {"role": "assistant", "content": "Hi there " * 1000},
            {"role": "user", "content": "recent message"},
            {"role": "assistant", "content": "recent response"},
        ]
        # keep_recent=2, so first 2 messages are "old"
        result = _compact_messages_with_summary(messages, keep_recent=2, text_limit=500)
        self.assertIsInstance(result, list)
        # Should have old messages (trimmed or summarized) + recent messages
        self.assertTrue(len(result) >= 2)
        # Last message should be intact
        self.assertEqual(result[-1]["content"], "recent response")

    def test_compact_messages_with_summary_short_list(self):
        """When messages fit within keep_recent, no summarization needed."""
        from src.gateway_app import _compact_messages_with_summary
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = _compact_messages_with_summary(messages, keep_recent=5, text_limit=1000)
        self.assertEqual(len(result), 2)

    def test_compact_messages_with_summary_empty(self):
        """Empty messages list returns empty."""
        from src.gateway_app import _compact_messages_with_summary
        result = _compact_messages_with_summary([], keep_recent=5, text_limit=1000)
        self.assertEqual(result, [])

    def test_summary_cache_hit(self):
        """Same messages should return cached summary."""
        from src.gateway_context import _summary_cache_put, _summarize_via_llm
        import hashlib
        # Pre-populate cache
        test_msgs = [{"role": "user", "content": "test"}]
        content_key = json.dumps(test_msgs, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(content_key.encode()).hexdigest()[:16]
        _summary_cache_put(content_hash, "cached summary")
        result = _summarize_via_llm(test_msgs)
        self.assertEqual(result, "cached summary")


class WeakUpstreamToolRoundSurfacingTests(unittest.TestCase):
    """Regression: weak upstreams that return no tool calls must still surface
    downstream-executed planner tool rounds to the downstream client across all
    three protocol paths (/v1/chat/completions, /v1/messages, /v1/responses)."""

    def _setup_workspace(self):
        td = tempfile.mkdtemp()
        readme = pathlib.Path(td) / "README.md"
        readme.write_text("# TestProject\n\nThis is a test.\n", encoding="utf-8")
        return td

    def _run(self, path, body, upstream_protocol="openai_chat"):
        """Run orchestration with a FakeClient that returns plain text (no tool calls)."""
        from src.gateway_config import save_config
        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        try:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["gateway"]["local_planner_enabled"] = True
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = upstream_protocol
            cfg["upstream"]["capabilities"] = {
                "supports_tools": False,
                "supports_function_calls": False,
            }
            gateway.save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            # Weak upstream returns plain assistant text, no tool calls
            if upstream_protocol == "anthropic_messages":
                weak_resp = {"id": "m1", "type": "message", "role": "assistant", "model": "weak",
                             "content": [{"type": "text", "text": "我来读取文件"}],
                             "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
            elif upstream_protocol == "openai_responses":
                weak_resp = {"id": "r1", "object": "response", "model": "weak", "status": "completed",
                             "output": [{"type": "message", "content": [{"type": "output_text", "text": "我来读取文件"}]}],
                             "usage": {"input_tokens": 1, "output_tokens": 1}}
            else:
                weak_resp = {"id": "c1", "choices": [{"message": {"role": "assistant", "content": "我来读取文件"},
                                                       "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

            client = FakeClient([weak_resp])
            result = run_tool_orchestration(path, body, client)
            return result
        finally:
            gateway.CONFIG_PATH = old_config
            os.chdir(old_cwd)
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws

    def test_chat_completions_surfaces_tool_rounds(self):
        result = self._run("/v1/chat/completions", {
            "model": "weak",
            "messages": [{"role": "user", "content": "分析这套项目 README.md"}],
            "tools": [{"type": "function", "function": {"name": "Read", "description": "read",
                       "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
        })
        choice = (result.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        self.assertTrue(bool(msg.get("tool_calls")), "should have tool_calls")
        self.assertEqual(choice.get("finish_reason"), "tool_calls")
        self.assertEqual((result.get("gateway_context") or {}).get("strategy"), "gateway_downstream_tool_request")

    def test_messages_surfaces_tool_use_blocks(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [{"role": "user", "content": "分析这套项目 README.md"}],
            "max_tokens": 4096,
        })
        content = result.get("content") or []
        tool_use = [b for b in content if b.get("type") == "tool_use"]
        self.assertTrue(bool(tool_use), "should have tool_use blocks")
        # tool_result blocks must NOT be in assistant message (protocol violation)
        tool_result = [b for b in content if b.get("type") == "tool_result"]
        self.assertFalse(tool_result, "tool_result should not be in assistant message")
        # stop_reason must be tool_use to signal client to send tool_result back
        self.assertEqual(result.get("stop_reason"), "tool_use")
        self.assertEqual((result.get("gateway_context") or {}).get("strategy"), "gateway_downstream_tool_request")

    def test_responses_surfaces_function_call_items(self):
        result = self._run("/v1/responses", {
            "model": "weak",
            "input": "分析这套项目 README.md",
        })
        output = result.get("output") or []
        fc = [o for o in output if o.get("type") == "function_call"]
        self.assertTrue(bool(fc), "should have function_call items")
        self.assertEqual((result.get("gateway_context") or {}).get("strategy"), "gateway_downstream_tool_request")

    def test_direct_read_preserves_context_through_protocol_conversion(self):
        """When direct_read fires, gateway_context.local_planner must survive
        _to_openai_chat_payload for the /v1/messages path."""
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [{"role": "user", "content": "读取 README.md"}],
            "max_tokens": 4096,
        })
        self.assertEqual((result.get("gateway_context") or {}).get("strategy"), "gateway_downstream_tool_request",
                         "downstream tool request marker must survive protocol conversion")

    def test_tool_result_in_request_prevents_re_surfacing(self):
        """When Claude Code sends back tool_result blocks (from a previous
        tool_use turn), the gateway must NOT re-surface planner tool rounds
        to avoid an infinite loop."""
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "分析这套项目 README.md"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Tree", "input": {"path": "."}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "tree output"},
                ]},
            ],
            "max_tokens": 4096,
        })
        # Should return upstream's plain text, NOT re-surface tool rounds
        self.assertNotEqual((result.get("gateway_context") or {}).get("strategy"),
                            "gateway_downstream_tool_request",
                            "must not re-surface tool rounds when tool_result is present")


class ToolCallDefaultTests(unittest.TestCase):
    """Tests that upstream APIs default to NOT supporting native tools.

    The gateway must default to text-tool-adapter mode for unknown upstreams,
    so that ANY API (even those without native tool support) can be used with
    tool-calling clients like Claude Code / Codex.
    """

    def setUp(self):
        self._old_config = gateway.CONFIG_PATH
        self._td = tempfile.TemporaryDirectory()
        gateway.CONFIG_PATH = pathlib.Path(self._td.name) / "config.json"
        cfg = gateway._default_config()
        gateway.save_config(cfg)

    def tearDown(self):
        gateway.CONFIG_PATH = self._old_config
        self._td.cleanup()

    def test_upstream_native_tools_capable_defaults_false(self):
        from src.gateway_streaming import _upstream_native_tools_capable
        self.assertFalse(_upstream_native_tools_capable(),
                         "Default _upstream_native_tools_capable must be False")

    def test_upstream_supports_native_tools_defaults_false(self):
        from src.gateway_context import _upstream_supports_native_tools
        self.assertFalse(_upstream_supports_native_tools(),
                         "Default _upstream_supports_native_tools must be False")

    def test_should_use_text_tool_adapter_defaults_true(self):
        from src.gateway_streaming import _should_use_text_tool_adapter, _tools_enabled_for_upstream, _upstream_native_tools_capable
        tools_enabled = _tools_enabled_for_upstream()
        native_capable = _upstream_native_tools_capable()
        self.assertEqual(tools_enabled, "auto")
        self.assertFalse(native_capable)
        self.assertTrue(_should_use_text_tool_adapter(tools_enabled, native_capable),
                        "Text tool adapter must be the default when upstream doesn't support native tools")

    def test_native_tools_capable_true_when_explicitly_configured(self):
        cfg = gateway.load_config()
        upstream = dict(cfg.get("upstream", {}))
        upstream["capabilities"] = {"supports_tools": True, "supports_function_calls": True}
        cfg["upstream"] = upstream
        gateway.save_config(cfg)

        from src.gateway_streaming import _upstream_native_tools_capable, _should_use_text_tool_adapter
        self.assertTrue(_upstream_native_tools_capable(),
                        "Must be True when explicitly configured")
        self.assertFalse(_should_use_text_tool_adapter("auto", True),
                         "Text adapter must not activate when native tools are configured")

    def test_text_tool_adapter_strips_tools_and_injects_prompt(self):
        """When upstream doesn't support native tools, _merge_builtin_tools strips
        native 'tools' from the body and injects them as text in the system prompt."""
        from src.gateway_streaming import _merge_builtin_tools

        body = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Read the README.md file"},
            ],
            "tools": [
                {"type": "function", "function": {"name": "Read", "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}}}},
            ],
            "max_tokens": 100,
        }

        result = _merge_builtin_tools("/v1/chat/completions", body)

        # Tools should be stripped from wire format (text adapter mode)
        self.assertNotIn("tools", result,
                         "Native tools must be stripped in text adapter mode")

        # System message must contain the tool adapter instructions
        system_content = str(result["messages"][0].get("content", ""))
        self.assertIn("Tool Call Gateway adapter", system_content,
                      "System prompt must contain tool adapter instructions")

        # User message must contain the reminder
        user_content = str(result["messages"][1].get("content", ""))
        self.assertIn("[IMPORTANT: Tool Call Gateway adapter is active", user_content,
                      "User message must contain adapter reminder")

    def test_text_tool_adapter_roundtrip_extracts_user_side_text_tool_calls(self):
        """End-to-end: request with tools → text adapter → upstream text with
        tool calls → extract user-side call for downstream execution."""
        from src.gateway_streaming import _merge_builtin_tools
        from src.gateway_tool_runtime import _extract_text_tool_calls, _extract_tool_calls

        body = {
            "model": "test-model",
            "messages": [
                {"role": "user", "content": "What files are in the current directory?"},
            ],
            "max_tokens": 100,
        }

        merged = _merge_builtin_tools("/v1/chat/completions", body)
        # Verify text adapter activated (no native tools in body)
        self.assertNotIn("tools", merged)

        # Simulate upstream response with text tool call
        response = {
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": (
                        "Let me list the files.\n\n"
                        "<function=Bash>\n"
                        "<parameter=command>ls -la</parameter>\n"
                        "</function>\n"
                    ),
                },
                "finish_reason": "stop",
            }],
        }

        # Native extraction should find nothing
        native_calls = _extract_tool_calls("/v1/chat/completions", response)
        self.assertEqual(len(native_calls), 0,
                         "No native tool calls expected from text-only response")

        # Text extraction should find the Bash call
        text_calls = _extract_text_tool_calls("/v1/chat/completions", response)
        self.assertGreater(len(text_calls), 0,
                           "Text tool call extraction must find <function=Bash> in response")
        self.assertEqual(text_calls[0].name, "Bash")
        self.assertIn("cmd", text_calls[0].arguments)
