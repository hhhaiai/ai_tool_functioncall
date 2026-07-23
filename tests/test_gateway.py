import unittest
import base64
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import uuid
import io
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from typing import Any

import src.toolcall_gateway as gateway

Json = dict[str, Any]
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
    def test_python_request_rate_limit_is_per_authenticated_client(self):
        from src.gateway_rate_limit import RATE_LIMIT_SERVICE

        class Handler:
            client_address = ("127.0.0.1", 12345)

        RATE_LIMIT_SERVICE.memory.clear()
        with patch("src.gateway_config._gateway_config", return_value={"rate_limit_enabled": True, "rate_limit_rpm": 2}):
            gateway._enforce_request_rate_limit(Handler(), "client-a")
            gateway._enforce_request_rate_limit(Handler(), "client-a")
            with self.assertRaises(gateway.GatewayBusyError):
                gateway._enforce_request_rate_limit(Handler(), "client-a")
            gateway._enforce_request_rate_limit(Handler(), "client-b")

    def test_capability_contract_is_truthful_about_compatibility_and_dormant_modules(self):
        contract = gateway._capability_contract()
        self.assertEqual(contract["api"]["assistants"], "persistent_gateway_owned_lifecycle")
        self.assertEqual(contract["api"]["threads"], "persistent_gateway_owned_lifecycle")
        self.assertTrue(contract["api"]["assistant_resources"]["messages"])
        self.assertTrue(contract["api"]["assistant_resources"]["runs"])
        self.assertTrue(contract["request_path"]["authenticated_client_rate_limit"])
        self.assertEqual(contract["request_path"]["rate_limit_backend"]["configured"], "sqlite")
        self.assertIn(contract["request_path"]["rate_limit_backend"]["active"], {"sqlite", "memory_fallback"})
        self.assertEqual(contract["request_path"]["gateway_concurrency_module"], "integrated_profile_pool")
        self.assertIn("profiles", contract["request_path"]["upstream_pool"])
        self.assertEqual(contract["operations"]["upstream_pool_status"], "/api/upstreams/status")
        self.assertEqual(contract["operations"]["config_ui"], "/ui/config")
        self.assertEqual(contract["operations"]["config_update"], "/api/config/update")
        self.assertEqual(contract["operations"]["cache_clear"], "/api/cache/clear")
        self.assertEqual(contract["request_path"]["web2api_module"], "authenticated_bounded_http_request_path")
        self.assertEqual(contract["streaming"]["orchestrated_safe_text"], "end_to_end_incremental")
        self.assertTrue(contract["streaming"]["client_disconnect_cancels_upstream"])

    def test_json_reader_rejects_malformed_and_non_object_bodies_as_bad_request(self):
        from src.gateway_errors import BadRequestError

        class Handler:
            def __init__(self, raw: bytes):
                self.headers = {"Content-Length": str(len(raw))}
                self.rfile = io.BytesIO(raw)

        with patch("src.gateway_http_handler._request_body_limit", return_value=1024):
            with self.assertRaises(BadRequestError):
                gateway._read_json(Handler(b'{"broken":'))
            with self.assertRaises(BadRequestError):
                gateway._read_json(Handler(b'[1, 2, 3]'))

    def test_semantic_cache_fingerprint_binds_full_request_contract(self):
        base = {
            "model": "model-a",
            "stream": False,
            "messages": [
                {"role": "system", "content": "answer tersely"},
                {"role": "user", "content": "same question"},
            ],
            "temperature": 0.2,
        }
        first = gateway._semantic_cache_request_fingerprint("/v1/chat/completions", base)
        same = gateway._semantic_cache_request_fingerprint("/v1/chat/completions", {**base, "stream": True})
        different_model = gateway._semantic_cache_request_fingerprint("/v1/chat/completions", {**base, "model": "model-b"})
        different_system = gateway._semantic_cache_request_fingerprint(
            "/v1/chat/completions",
            {**base, "messages": [{"role": "system", "content": "answer expansively"}, base["messages"][1]]},
        )
        different_path = gateway._semantic_cache_request_fingerprint("/v1/messages", base)
        self.assertEqual(first, same)
        self.assertNotEqual(first, different_model)
        self.assertNotEqual(first, different_system)
        self.assertNotEqual(first, different_path)

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

    def test_extracts_legacy_chat_function_call_and_appends_function_result(self):
        response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "function_call": {
                        "name": "calculator",
                        "arguments": "{\"expression\":\"6*7\"}",
                    },
                },
                "finish_reason": "function_call",
            }]
        }

        self.assertTrue(_native_tool_signal("/v1/chat/completions", response))
        calls = _extract_tool_calls("/v1/chat/completions", response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "calculator")
        self.assertEqual(calls[0].arguments, {"expression": "6*7"})

        tool_result = _execute_tool_call(calls[0])
        self.assertTrue(tool_result.success)
        updated = _append_tool_results(
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "calc"}]},
            response,
            [tool_result],
        )
        self.assertEqual(updated["messages"][-2]["function_call"]["name"], "calculator")
        self.assertEqual(updated["messages"][-1]["role"], "function")
        self.assertEqual(updated["messages"][-1]["name"], "calculator")
        self.assertEqual(updated["messages"][-1]["content"], "42")

    def test_legacy_chat_function_result_becomes_planner_evidence(self):
        from src.gateway_agent_planner import extract_tool_evidence

        response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "function_call": {
                        "name": "calculator",
                        "arguments": "{\"expression\":\"6*7\"}",
                    },
                },
                "finish_reason": "function_call",
            }]
        }
        calls = _extract_tool_calls("/v1/chat/completions", response)
        tool_result = _execute_tool_call(calls[0])
        updated = _append_tool_results(
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "calc"}]},
            response,
            [tool_result],
        )

        evidence = extract_tool_evidence("/v1/chat/completions", updated)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].call_id, "legacy_function_call_calculator")
        self.assertEqual(evidence[0].name, "calculator")
        self.assertIn('"expression": "6*7"', evidence[0].content)
        self.assertTrue(evidence[0].content.endswith("42"))


    def test_failed_chat_tool_result_marks_planner_evidence_error(self):
        from src.gateway_agent_planner import extract_tool_evidence

        response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_fail",
                        "type": "function",
                        "function": {"name": "danger_tool", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }
        updated = _append_tool_results(
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "run danger"}]},
            response,
            [gateway.ToolResult(
                call_id="call_fail",
                name="danger_tool",
                content="permission denied",
                success=False,
                failure_type="permission_denied",
            )],
        )

        self.assertIn("[gateway_tool_result_error]", updated["messages"][-1]["content"])
        evidence = extract_tool_evidence("/v1/chat/completions", updated)
        self.assertTrue(evidence[-1].is_error)
        self.assertEqual(evidence[-1].content, "permission denied")

    def test_failed_chat_tool_result_marker_is_not_forwarded_to_final_synthesis(self):
        from src.gateway_agent_planner import prepare_upstream_body

        response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_fail",
                        "type": "function",
                        "function": {"name": "danger_tool", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }
        updated = _append_tool_results(
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "run danger"}]},
            response,
            [gateway.ToolResult(
                call_id="call_fail",
                name="danger_tool",
                content="permission denied",
                success=False,
                failure_type="permission_denied",
            )],
        )

        prepared = prepare_upstream_body("/v1/chat/completions", updated)
        tool_messages = [msg for msg in prepared["messages"] if isinstance(msg, dict) and msg.get("role") == "tool"]
        self.assertEqual(tool_messages[-1]["content"], "permission denied")
        self.assertNotIn("[gateway_tool_result_error]", json.dumps(prepared, ensure_ascii=False))

    def test_failed_responses_tool_output_marks_planner_evidence_error(self):
        from src.gateway_agent_planner import extract_tool_evidence

        response = {
            "output": [{
                "type": "function_call",
                "call_id": "call_fail_resp",
                "name": "danger_tool",
                "arguments": "{}",
            }]
        }
        updated = _append_tool_results(
            "/v1/responses",
            {"input": "run danger"},
            response,
            [gateway.ToolResult(
                call_id="call_fail_resp",
                name="danger_tool",
                content="permission denied",
                success=False,
                failure_type="permission_denied",
            )],
        )

        self.assertIn("[gateway_tool_result_error]", updated["input"][-1]["output"])
        evidence = extract_tool_evidence("/v1/responses", updated)
        self.assertTrue(evidence[-1].is_error)
        self.assertEqual(evidence[-1].content, "permission denied")

    def test_responses_function_call_output_becomes_planner_evidence_with_name_and_args(self):
        from src.gateway_agent_planner import extract_tool_evidence

        updated = _append_tool_results(
            "/v1/responses",
            {"input": "calc"},
            {"output": [{
                "type": "function_call",
                "call_id": "call_calc",
                "name": "calculator",
                "arguments": "{\"expression\":\"6*7\"}",
            }]},
            [gateway.ToolResult(
                call_id="call_calc",
                name="calculator",
                content="42",
                success=True,
            )],
        )

        evidence = extract_tool_evidence("/v1/responses", updated)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].call_id, "call_calc")
        self.assertEqual(evidence[0].name, "calculator")
        self.assertIn('"expression": "6*7"', evidence[0].content)
        self.assertTrue(evidence[0].content.endswith("42"))

    def test_responses_custom_tool_output_becomes_planner_evidence_with_string_input(self):
        from src.gateway_agent_planner import extract_tool_evidence

        updated = _append_tool_results(
            "/v1/responses",
            {"input": "calc"},
            {"output": [{
                "type": "custom_tool_call",
                "call_id": "call_custom",
                "name": "calculator",
                "input": "40+2",
            }]},
            [gateway.ToolResult(
                call_id="call_custom",
                name="calculator",
                content="42",
                success=True,
            )],
        )

        evidence = extract_tool_evidence("/v1/responses", updated)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].call_id, "call_custom")
        self.assertEqual(evidence[0].name, "calculator")
        self.assertIn('"input": "40+2"', evidence[0].content)
        self.assertTrue(evidence[0].content.endswith("42"))


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

    def test_declared_gateway_builtin_name_is_downstream_owned(self):
        from src.gateway_tool_runtime import _tool_call_requires_downstream_execution

        body = {
            "tools": [{
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "Client-side calculator function",
                    "parameters": {
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                    },
                },
            }],
        }

        self.assertTrue(
            _tool_call_requires_downstream_execution(
                ToolCall("call_client_calc", "calculator", {"expression": "6*7"}, {}),
                body,
            )
        )

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
            self.assertEqual(cfg["downstream_keys"][0]["prefix"], gateway._secret_fingerprint("env-downstream-key"))

            os.environ["GATEWAY_DOWNSTREAM_KEY"] = "gateway-key"
            cfg = gateway._default_config()
            self.assertEqual(cfg["gateway"]["client_snippet_api_key"], "env-downstream-key")
            self.assertEqual(cfg["downstream_keys"][0]["prefix"], gateway._secret_fingerprint("gateway-key"))
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
                self.assertTrue(cfg["admin"]["password_hash"].startswith("pbkdf2_sha256$"))
                self.assertTrue(gateway._verify_password("configured-admin-pass", cfg["admin"]["password_hash"]))
                self.assertNotIn("password", cfg["admin"])

                gateway.save_config(cfg)
                saved = json.loads(gateway.CONFIG_PATH.read_text(encoding="utf-8"))
                self.assertTrue(saved["admin"]["password_hash"].startswith("pbkdf2_sha256$"))
                self.assertTrue(gateway._verify_password("configured-admin-pass", saved["admin"]["password_hash"]))
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
                self.assertEqual(repaired["prefix"], gateway._secret_fingerprint("disabled-existing-key"))
                self.assertEqual(repaired["protocols"], ["models", "chat_completions", "responses", "messages", "direct_tools"])
            finally:
                gateway.CONFIG_PATH = old_config

    def test_public_runtime_templates_keep_safe_defaults(self):
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        template = json.loads((repo_root / "gateway.config.json").read_text(encoding="utf-8"))
        self.assertNotIn("workspace_root", template["gateway"])
        self.assertFalse(template["gateway"]["allow_write_tools"])
        self.assertFalse(template["gateway"]["allow_shell_tools"])
        self.assertEqual(template["gateway"].get("max_request_body_bytes"), 64 * 1024 * 1024)
        self.assertEqual(template["gateway"].get("max_log_payload_chars"), 200000)
        self.assertFalse(template["gateway"].get("cors_enabled"))
        self.assertEqual(template["gateway"].get("cors_allowed_origins"), [])

        yaml_text = (repo_root / "gateway.config.yaml").read_text(encoding="utf-8")
        self.assertIn("# workspace_root: /absolute/client/workspace", yaml_text)
        self.assertIn("allow_write_tools: false", yaml_text)
        self.assertIn("allow_shell_tools: false", yaml_text)
        self.assertIn("max_request_body_bytes: 67108864", yaml_text)
        self.assertIn("max_log_payload_chars: 200000", yaml_text)
        self.assertIn("cors_enabled: false", yaml_text)

        env_example = (repo_root / ".env.example").read_text(encoding="utf-8")
        dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
        compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        prod_compose = (repo_root / "docker-compose.prod.yml").read_text(encoding="utf-8")
        self.assertNotIn("GATEWAY_ADMIN_PASSWORD=admin", dockerfile)
        self.assertIn("GATEWAY_MAX_REQUEST_BODY_BYTES=67108864", env_example)
        self.assertIn("GATEWAY_MAX_LOG_PAYLOAD_CHARS=200000", env_example)
        self.assertIn("GATEWAY_CORS_ENABLED=0", env_example)
        self.assertIn("GATEWAY_MAX_REQUEST_BODY_BYTES=${GATEWAY_MAX_REQUEST_BODY_BYTES:-67108864}", compose)
        self.assertIn("GATEWAY_MAX_LOG_PAYLOAD_CHARS=${GATEWAY_MAX_LOG_PAYLOAD_CHARS:-200000}", compose)
        self.assertIn("GATEWAY_MAX_REQUEST_BODY_BYTES=${GATEWAY_MAX_REQUEST_BODY_BYTES:-67108864}", prod_compose)
        self.assertIn("GATEWAY_MAX_LOG_PAYLOAD_CHARS=${GATEWAY_MAX_LOG_PAYLOAD_CHARS:-200000}", prod_compose)
        self.assertIn("GATEWAY_ADMIN_PASSWORD=${GATEWAY_ADMIN_PASSWORD:-}", compose)
        self.assertIn("GATEWAY_ADMIN_PASSWORD=${GATEWAY_ADMIN_PASSWORD:?set GATEWAY_ADMIN_PASSWORD}", prod_compose)

    def test_failed_responses_tool_output_marker_is_not_forwarded_to_final_synthesis(self):
        from src.gateway_agent_planner import prepare_upstream_body

        updated = _append_tool_results(
            "/v1/responses",
            {"input": "run danger"},
            {"output": [{"type": "function_call", "call_id": "call_fail", "name": "danger_tool", "arguments": "{}"}]},
            [gateway.ToolResult(
                call_id="call_fail",
                name="danger_tool",
                content="permission denied",
                success=False,
                failure_type="permission_denied",
            )],
        )

        prepared = prepare_upstream_body("/v1/responses", updated)
        outputs = [item for item in prepared["input"] if isinstance(item, dict) and item.get("type") == "function_call_output"]
        self.assertEqual(outputs[-1]["output"], "permission denied")
        self.assertNotIn("[gateway_tool_result_error]", json.dumps(prepared, ensure_ascii=False))

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

    def test_responses_tool_response_sets_chat_finish_reason_tool_calls(self):
        from src.gateway_protocol import _from_responses_response_to_openai

        converted = _from_responses_response_to_openai({
            "id": "resp_tool",
            "output": [{
                "type": "function_call",
                "call_id": "call_1",
                "name": "calculator",
                "arguments": "{\"expression\":\"6*7\"}",
            }],
        })

        self.assertEqual(converted["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(converted["choices"][0]["message"]["tool_calls"][0]["id"], "call_1")

    def test_responses_custom_tool_response_converts_to_chat_tool_call(self):
        from src.gateway_protocol import _from_responses_response_to_openai

        converted = _from_responses_response_to_openai({
            "id": "resp_custom",
            "output": [{
                "type": "custom_tool_call",
                "call_id": "call_custom_1",
                "name": "calculator",
                "input": "40+2",
            }],
        })

        tool_call = converted["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool_call["id"], "call_custom_1")
        self.assertEqual(tool_call["function"]["name"], "calculator")
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), {"input": "40+2"})

    def test_response_has_tool_calls_detects_responses_custom_tool_call(self):
        from src.gateway_tool_runtime import _response_has_tool_calls

        self.assertTrue(_response_has_tool_calls("/v1/responses", {
            "output": [{
                "type": "custom_tool_call",
                "call_id": "call_custom_1",
                "name": "calculator",
                "input": "40+2",
            }],
        }))

    def test_responses_custom_tool_history_converts_to_chat_messages(self):
        from src.gateway_protocol import _convert_request_to_upstream

        _, converted = _convert_request_to_upstream(
            "/v1/responses",
            {
                "model": "m",
                "input": [
                    {
                        "type": "custom_tool_call",
                        "call_id": "call_custom_1",
                        "name": "calculator",
                        "input": "40+2",
                    },
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_custom_1",
                        "name": "calculator",
                        "output": "42",
                    },
                ],
            },
            "openai_chat",
        )

        self.assertEqual(converted["messages"][0]["role"], "assistant")
        self.assertEqual(converted["messages"][0]["tool_calls"][0]["id"], "call_custom_1")
        self.assertEqual(converted["messages"][0]["tool_calls"][0]["function"]["name"], "calculator")
        self.assertEqual(
            json.loads(converted["messages"][0]["tool_calls"][0]["function"]["arguments"]),
            {"input": "40+2"},
        )
        self.assertEqual(converted["messages"][1], {
            "role": "tool",
            "tool_call_id": "call_custom_1",
            "content": "42",
        })

    def test_responses_codex_builtin_tool_history_converts_to_chat_messages(self):
        from src.gateway_protocol import _convert_request_to_upstream

        _, converted = _convert_request_to_upstream(
            "/v1/responses",
            {
                "model": "m",
                "input": [
                    {
                        "type": "local_shell_call",
                        "call_id": "call_shell_1",
                        "action": {"command": "pwd"},
                    },
                    {
                        "type": "local_shell_call_output",
                        "call_id": "call_shell_1",
                        "output": "/workspace/project",
                    },
                    {
                        "type": "tool_search_call",
                        "id": "call_search_1",
                        "action": {"query": "pytest failures"},
                    },
                    {
                        "type": "tool_search_output",
                        "id": "call_search_1",
                        "content": "no failures",
                    },
                ],
            },
            "openai_chat",
        )

        self.assertEqual(converted["messages"][0]["role"], "assistant")
        shell_call = converted["messages"][0]["tool_calls"][0]
        self.assertEqual(shell_call["id"], "call_shell_1")
        self.assertEqual(shell_call["function"]["name"], "local_shell")
        self.assertEqual(json.loads(shell_call["function"]["arguments"]), {"command": "pwd"})
        self.assertEqual(converted["messages"][1], {
            "role": "tool",
            "tool_call_id": "call_shell_1",
            "content": "/workspace/project",
        })
        search_call = converted["messages"][2]["tool_calls"][0]
        self.assertEqual(search_call["id"], "call_search_1")
        self.assertEqual(search_call["function"]["name"], "tool_search")
        self.assertEqual(json.loads(search_call["function"]["arguments"]), {"query": "pytest failures"})
        self.assertEqual(converted["messages"][3], {
            "role": "tool",
            "tool_call_id": "call_search_1",
            "content": "no failures",
        })

    def test_responses_codex_builtin_tool_response_converts_to_chat_tool_call(self):
        from src.gateway_protocol import _from_responses_response_to_openai

        converted = _from_responses_response_to_openai({
            "id": "resp_shell",
            "output": [{
                "type": "local_shell_call",
                "call_id": "call_shell_1",
                "action": {"command": "pwd"},
            }],
        })

        self.assertEqual(converted["choices"][0]["finish_reason"], "tool_calls")
        tool_call = converted["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool_call["id"], "call_shell_1")
        self.assertEqual(tool_call["function"]["name"], "local_shell")
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), {"command": "pwd"})

    def test_responses_codex_builtin_tool_output_becomes_planner_evidence(self):
        from src.gateway_agent_planner import extract_tool_evidence

        body = {
            "input": [
                {
                    "type": "local_shell_call",
                    "call_id": "call_shell_1",
                    "action": {"command": "pwd"},
                },
                {
                    "type": "local_shell_call_output",
                    "call_id": "call_shell_1",
                    "output": "/workspace/project",
                },
            ]
        }

        evidence = extract_tool_evidence("/v1/responses", body)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].call_id, "call_shell_1")
        self.assertEqual(evidence[0].name, "local_shell")
        self.assertIn('"command": "pwd"', evidence[0].content)
        self.assertTrue(evidence[0].content.endswith("/workspace/project"))

    def test_gateway_internal_workspace_fields_are_not_forwarded_upstream(self):
        from src.gateway_protocol import _convert_request_to_upstream

        body = {
            "model": "m",
            "workspace_root": "/tmp/secret-project",
            "gateway_workspace": "/tmp/secret-project",
            "workspace": "/tmp/secret-project",
            "workspace_dir": "/tmp/secret-project",
            "projectDir": "/tmp/secret-project",
            "cwd": "/tmp/secret-project",
            "gateway_context": {"agent_planner": {"session_key": "secret-session"}},
            "gateway_agent_planner": {"evidence_injected": True},
            "metadata": {
                "session_id": "s1",
                "workspace_root": "/tmp/secret-project",
                "gateway_workspace": "/tmp/secret-project",
                "workspace": "/tmp/secret-project",
                "workspace_dir": "/tmp/secret-project",
                "projectDir": "/tmp/secret-project",
                "user_id": json.dumps({
                    "session_id": "s1",
                    "cwd": "/tmp/secret-project",
                    "workspace": "/tmp/secret-project",
                    "workspace_dir": "/tmp/secret-project",
                }),
            },
            "messages": [{"role": "user", "content": "hello"}],
        }

        _, converted = _convert_request_to_upstream("/v1/chat/completions", body, "openai_chat")

        serialized = json.dumps(converted, ensure_ascii=False)
        self.assertNotIn("/tmp/secret-project", serialized)
        self.assertNotIn("workspace_root", converted)
        self.assertNotIn("gateway_workspace", converted)
        self.assertNotIn("workspace", converted)
        self.assertNotIn("workspace_dir", converted)
        self.assertNotIn("gateway_context", converted)
        self.assertNotIn("gateway_agent_planner", converted)
        self.assertEqual(converted["metadata"]["session_id"], "s1")
        self.assertEqual(json.loads(converted["metadata"]["user_id"]), {"session_id": "s1"})

        _, converted_string_metadata = _convert_request_to_upstream(
            "/v1/chat/completions",
            {
                "model": "m",
                "metadata": json.dumps({
                    "session_id": "s2",
                    "workspace_root": "/tmp/secret-project",
                    "workspace": "/tmp/secret-project",
                    "workspace_dir": "/tmp/secret-project",
                }),
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
                        "workspace": "/tmp/secret-project",
                        "workspace_dir": "/tmp/secret-project",
                        "metadata": {
                            "session_id": "s1",
                            "workspace_root": "/tmp/secret-project",
                            "workspace": "/tmp/secret-project",
                            "workspace_dir": "/tmp/secret-project",
                            "user_id": json.dumps({
                                "session_id": "s1",
                                "cwd": "/tmp/secret-project",
                                "workspace": "/tmp/secret-project",
                                "workspace_dir": "/tmp/secret-project",
                            }),
                        },
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
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = True
                gateway.save_config(cfg)
                pathlib.Path(other, "app.py").write_text("print('other')\n", encoding="utf-8")
                result = execute_direct_tool_call(
                    {"workspace_root": other, "tool": "Read", "arguments": {"file_path": "app.py"}, "call_id": "scoped"}
                )
                self.assertTrue(result["success"])
                self.assertIn("other", result["content"])
            finally:
                gateway.CONFIG_PATH = old_config

    def test_direct_user_side_tool_call_requires_downstream_client_by_default(self):
        from src.gateway_errors import BadRequestError

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = td
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = False
                gateway.save_config(cfg)
                pathlib.Path(other, "secret.txt").write_text("gateway-service-secret\n", encoding="utf-8")
                pathlib.Path(other, "secret.json").write_text('{"secret":"gateway-service-secret"}', encoding="utf-8")

                cases = [
                    ("Read", {"file_path": "secret.txt"}),
                    ("Bash", {"command": "cat secret.txt"}),
                    ("computer_use", {}),
                    ("click", {"x": 1, "y": 1}),
                    ("Agent", {"prompt": "read secret.txt"}),
                    ("Skill", {"name": "read_skill"}),
                    ("JsonQuery", {"file_path": "secret.json", "query": "secret"}),
                ]
                for tool_name, arguments in cases:
                    with self.subTest(tool_name=tool_name):
                        with self.assertRaises(BadRequestError) as raised:
                            execute_direct_tool_call(
                                {
                                    "workspace_root": other,
                                    "tool": tool_name,
                                    "arguments": arguments,
                                    "call_id": f"direct_{tool_name}_blocked",
                                }
                            )

                        self.assertEqual(
                            raised.exception.detail["failure_type"],
                            "direct_user_side_tool_requires_downstream_client",
                        )
                        self.assertEqual(raised.exception.detail["tool_names"], [tool_name])
                        self.assertNotIn("gateway-service-secret", str(raised.exception))
                        self.assertNotIn("gateway-service-secret", json.dumps(raised.exception.detail, ensure_ascii=False))

                data_query = execute_direct_tool_call(
                    {"tool": "JsonQuery", "arguments": {"data": {"safe": {"answer": 42}}, "query": "safe.answer"}}
                )
                self.assertTrue(data_query["success"])
                self.assertEqual(data_query["content"], "42")

                parallel_cases = [
                    {
                        "workspace_root": other,
                        "tool": "multi_tool_use.parallel",
                        "arguments": {"tool_uses": [{"recipient_name": "functions.Read", "parameters": {"file_path": "secret.txt"}}]},
                    },
                    {
                        "workspace_root": other,
                        "tool": "parallel",
                        "arguments": {"tool_uses": [{"recipient_name": "functions.Agent", "parameters": {"prompt": "read secret.txt"}}]},
                    },
                    {
                        "workspace_root": other,
                        "tool": "multi_tool_use.parallel",
                        "arguments": {"tool_uses": [{"recipient_name": "functions.JsonQuery", "parameters": {"file_path": "secret.json", "query": "secret"}}]},
                    },
                    {
                        "workspace_root": other,
                        "tool_uses": [{"recipient_name": "functions.Bash", "parameters": {"command": "cat secret.txt"}}],
                    },
                ]
                for body in parallel_cases:
                    with self.subTest(parallel_body=body.get("tool") or "tool_uses"):
                        with self.assertRaises(BadRequestError) as raised:
                            execute_direct_tool_call(body)
                        self.assertEqual(
                            raised.exception.detail["failure_type"],
                            "direct_user_side_tool_requires_downstream_client",
                        )
                        self.assertNotIn("gateway-service-secret", str(raised.exception))
                        self.assertNotIn("gateway-service-secret", json.dumps(raised.exception.detail, ensure_ascii=False))
            finally:
                gateway.CONFIG_PATH = old_config

    def test_declared_downstream_file_path_does_not_use_gateway_env_without_client_workspace(self):
        from src.gateway_agent_planner import _adapt_args as planner_adapt_args
        from src.gateway_tool_runtime import _adapt_arguments_for_declared_tool, _workspace_scope

        with tempfile.TemporaryDirectory() as service_root:
            body = {
                "tools": [{
                    "name": "Read",
                    "input_schema": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                        "additionalProperties": False,
                    },
                }]
            }
            with _workspace_scope(pathlib.Path(service_root), body):
                runtime_args = _adapt_arguments_for_declared_tool(body, "Read", {"path": "README.md"})
                planner_args = planner_adapt_args(body, "Read", {"path": "README.md"})
                runtime_direct_args = _adapt_arguments_for_declared_tool(body, "Read", {"file_path": "README.md"})
                planner_direct_args = planner_adapt_args(body, "Read", {"file_path": "README.md"})

            self.assertEqual(runtime_args["file_path"], "README.md")
            self.assertEqual(planner_args["file_path"], "README.md")
            self.assertEqual(runtime_direct_args["file_path"], "README.md")
            self.assertEqual(planner_direct_args["file_path"], "README.md")
            self.assertNotIn(service_root, json.dumps(runtime_args, ensure_ascii=False))
            self.assertNotIn(service_root, json.dumps(planner_args, ensure_ascii=False))
            self.assertNotIn(service_root, json.dumps(runtime_direct_args, ensure_ascii=False))
            self.assertNotIn(service_root, json.dumps(planner_direct_args, ensure_ascii=False))

    def test_declared_downstream_file_path_anchors_to_explicit_client_workspace(self):
        from src.gateway_agent_planner import _adapt_args as planner_adapt_args
        from src.gateway_tool_runtime import _adapt_arguments_for_declared_tool, _workspace_scope

        client_root = pathlib.Path("/tmp/client-project-for-gateway-tests")
        body = {
            "metadata": {"workspace": str(client_root)},
            "tools": [{
                "name": "Read",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            }],
        }
        with _workspace_scope(client_root, body):
            runtime_args = _adapt_arguments_for_declared_tool(body, "Read", {"path": "README.md"})
            planner_args = planner_adapt_args(body, "Read", {"path": "README.md"})
            runtime_direct_args = _adapt_arguments_for_declared_tool(body, "Read", {"file_path": "README.md"})
            planner_direct_args = planner_adapt_args(body, "Read", {"file_path": "README.md"})

        expected = str((client_root / "README.md").resolve(strict=False))
        self.assertEqual(runtime_args["file_path"], expected)
        self.assertEqual(planner_args["file_path"], expected)
        self.assertEqual(runtime_direct_args["file_path"], expected)
        self.assertEqual(planner_direct_args["file_path"], expected)

    def test_relative_workspace_value_is_not_treated_as_gateway_service_path(self):
        from src.gateway_agent_planner import _adapt_args as planner_adapt_args
        from src.gateway_tool_runtime import _adapt_arguments_for_declared_tool, _request_workspace_root, _workspace_scope

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_cwd = os.getcwd()
            service_root = pathlib.Path(td) / "service"
            service_root.mkdir()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                gateway.save_config(cfg)
                os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
                os.chdir(service_root)
                body = {
                    "workspace": "relative-client-id",
                    "metadata": {"session_id": "relative-workspace-session", "user_id": "relative-workspace-user"},
                    "tools": [{
                        "name": "Read",
                        "input_schema": {
                            "type": "object",
                            "properties": {"file_path": {"type": "string"}},
                            "required": ["file_path"],
                            "additionalProperties": False,
                        },
                    }],
                }
                root = _request_workspace_root(body)
                self.assertNotEqual(root, (service_root / "relative-client-id").resolve(strict=False))
                self.assertNotEqual(root, service_root.resolve(strict=False))
                self.assertIn("anonymous_spaces", str(root))
                sibling_body = dict(body)
                sibling_body["workspace"] = "relative-client-id-2"
                sibling_root = _request_workspace_root(sibling_body)
                self.assertNotEqual(root, sibling_root)
                with _workspace_scope(root, body):
                    runtime_args = _adapt_arguments_for_declared_tool(body, "Read", {"path": "README.md"})
                    planner_args = planner_adapt_args(body, "Read", {"path": "README.md"})
                    runtime_direct_args = _adapt_arguments_for_declared_tool(body, "Read", {"file_path": "README.md"})
                    planner_direct_args = planner_adapt_args(body, "Read", {"file_path": "README.md"})
                self.assertEqual(runtime_args["file_path"], "README.md")
                self.assertEqual(planner_args["file_path"], "README.md")
                self.assertEqual(runtime_direct_args["file_path"], "README.md")
                self.assertEqual(planner_direct_args["file_path"], "README.md")
                self.assertNotIn(str(service_root), json.dumps(runtime_args, ensure_ascii=False))
                self.assertNotIn(str(service_root), json.dumps(planner_args, ensure_ascii=False))
                self.assertNotIn(str(service_root), json.dumps(runtime_direct_args, ensure_ascii=False))
                self.assertNotIn(str(service_root), json.dumps(planner_direct_args, ensure_ascii=False))
            finally:
                gateway.CONFIG_PATH = old_config
                os.chdir(old_cwd)
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_parallel_direct_tool_calls_keep_client_workspaces_isolated(self):
        import concurrent.futures

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = True
                gateway.save_config(cfg)
                workspaces = []
                for idx in range(4):
                    root = pathlib.Path(td) / f"client-{idx}"
                    root.mkdir()
                    (root / "marker.txt").write_text(f"client-{idx}", encoding="utf-8")
                    workspaces.append(root)

                def read_marker(root: pathlib.Path) -> str:
                    result = execute_direct_tool_call(
                        {"workspace_root": str(root), "tool": "Read", "arguments": {"file_path": "marker.txt"}, "call_id": str(root.name)}
                    )
                    self.assertTrue(result["success"])
                    return result["content"]

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(read_marker, workspaces))

                for idx, content in enumerate(results):
                    self.assertIn(f"client-{idx}", content)
                    for other_idx in range(4):
                        if other_idx != idx:
                            self.assertNotIn(f"client-{other_idx}", content)
            finally:
                gateway.CONFIG_PATH = old_config

    def test_remote_exec_sessions_are_scoped_by_client_workspace_and_tenant(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["allow_shell_tools"] = True
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = True
                gateway.save_config(cfg)
                workspace_a = pathlib.Path(td) / "client-a"
                workspace_b = pathlib.Path(td) / "client-b"
                workspace_a.mkdir()
                workspace_b.mkdir()
                (workspace_a / "marker.txt").write_text("CLIENT-A", encoding="utf-8")
                (workspace_b / "marker.txt").write_text("CLIENT-B", encoding="utf-8")
                command = (
                    "python3 -u -c \"import sys; "
                    "line=sys.stdin.readline().strip(); "
                    "print(open('marker.txt').read().strip(), flush=True); "
                    "print(line, flush=True)\""
                )

                base = {
                    "tool": "exec_shell_start",
                    "arguments": {"session_id": "shared-shell", "command": command},
                }
                start_a = execute_direct_tool_call(
                    {
                        **base,
                        "workspace_root": str(workspace_a),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-a"})},
                    }
                )
                start_b = execute_direct_tool_call(
                    {
                        **base,
                        "workspace_root": str(workspace_b),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-b"})},
                    }
                )
                self.assertTrue(start_a["success"], start_a)
                self.assertTrue(start_b["success"], start_b)

                write_a = execute_direct_tool_call(
                    {
                        "workspace_root": str(workspace_a),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-a"})},
                        "tool": "write_stdin",
                        "arguments": {"session_id": "shared-shell", "chars": "hello-a\n", "read_timeout": 0.2},
                    }
                )
                write_b = execute_direct_tool_call(
                    {
                        "workspace_root": str(workspace_b),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-b"})},
                        "tool": "write_stdin",
                        "arguments": {"session_id": "shared-shell", "chars": "hello-b\n", "read_timeout": 0.2},
                    }
                )
                self.assertTrue(write_a["success"], write_a)
                self.assertTrue(write_b["success"], write_b)
                self.assertIn("CLIENT-A", write_a["content"])
                self.assertIn("hello-a", write_a["content"])
                self.assertNotIn("CLIENT-B", write_a["content"])
                self.assertIn("CLIENT-B", write_b["content"])
                self.assertIn("hello-b", write_b["content"])
                self.assertNotIn("CLIENT-A", write_b["content"])
            finally:
                gateway.CONFIG_PATH = old_config

    def test_remote_team_mailboxes_are_scoped_by_client_workspace_and_tenant(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                gateway.save_config(gateway._default_config())
                workspace_a = pathlib.Path(td) / "client-a"
                workspace_b = pathlib.Path(td) / "client-b"
                workspace_a.mkdir()
                workspace_b.mkdir()
                common_args = {"id": "shared-team", "name": "shared-team"}
                create_a = execute_direct_tool_call(
                    {
                        "workspace_root": str(workspace_a),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-a"})},
                        "tool": "TeamCreate",
                        "arguments": common_args,
                    }
                )
                create_b = execute_direct_tool_call(
                    {
                        "workspace_root": str(workspace_b),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-b"})},
                        "tool": "TeamCreate",
                        "arguments": common_args,
                    }
                )
                self.assertTrue(create_a["success"], create_a)
                self.assertTrue(create_b["success"], create_b)

                send_a = execute_direct_tool_call(
                    {
                        "workspace_root": str(workspace_a),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-a"})},
                        "tool": "SendMessage",
                        "arguments": {"target": "shared-team", "message": "only-a"},
                    }
                )
                delete_b = execute_direct_tool_call(
                    {
                        "workspace_root": str(workspace_b),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-b"})},
                        "tool": "TeamDelete",
                        "arguments": {"id": "shared-team"},
                    }
                )
                delete_a = execute_direct_tool_call(
                    {
                        "workspace_root": str(workspace_a),
                        "metadata": {"session_id": "same-session", "user_id": json.dumps({"user_id": "user-a"})},
                        "tool": "TeamDelete",
                        "arguments": {"id": "shared-team"},
                    }
                )
                self.assertTrue(send_a["success"], send_a)
                self.assertTrue(delete_b["success"], delete_b)
                self.assertNotIn("only-a", delete_b["content"])
                self.assertTrue(delete_a["success"], delete_a)
                self.assertIn("only-a", delete_a["content"])
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
            old_exec = os.environ.get("GATEWAY_EXECUTE_USER_SIDE_TOOLS")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_EXECUTE_USER_SIDE_TOOLS"] = "1"
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
                if old_exec is None:
                    os.environ.pop("GATEWAY_EXECUTE_USER_SIDE_TOOLS", None)
                else:
                    os.environ["GATEWAY_EXECUTE_USER_SIDE_TOOLS"] = old_exec

    def test_code_interpreter_is_real_but_permission_gated(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["allow_shell_tools"] = False
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = True
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

    def test_remote_anonymous_workspace_is_not_shared_by_identical_prompts(self):
        from src.gateway_tool_runtime import _request_workspace_root

        body = {"model": "m", "messages": [{"role": "user", "content": "same prompt from remote user"}]}
        root_a = _request_workspace_root(body)
        root_b = _request_workspace_root(body)
        self.assertNotEqual(root_a, root_b)
        self.assertIn("anonymous_spaces", str(root_a))
        self.assertIn("anonymous_spaces", str(root_b))

    def test_remote_anonymous_workspace_is_tenant_session_scoped(self):
        from src.gateway_tool_runtime import _request_workspace_root

        body_a1 = {"metadata": {"session_id": "shared-session", "user_id": json.dumps({"user_id": "user-a"})}}
        body_a2 = {"metadata": {"session_id": "shared-session", "user_id": json.dumps({"user_id": "user-a"})}}
        body_b = {"metadata": {"session_id": "shared-session", "user_id": json.dumps({"user_id": "user-b"})}}
        root_a1 = _request_workspace_root(body_a1)
        root_a2 = _request_workspace_root(body_a2)
        root_b = _request_workspace_root(body_b)
        self.assertEqual(root_a1, root_a2)
        self.assertNotEqual(root_a1, root_b)

    def test_remote_identity_without_workspace_does_not_fall_back_to_gateway_env_root(self):
        from src.gateway_tool_runtime import _request_workspace_root

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            service_root = pathlib.Path(td) / "gateway-service-root"
            service_root.mkdir()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                gateway.save_config(cfg)

                body = {"metadata": {"session_id": "remote-session", "user_id": json.dumps({"user_id": "remote-user"})}}
                root_a = _request_workspace_root(body)
                root_b = _request_workspace_root(body)

                self.assertEqual(root_a, root_b)
                self.assertNotEqual(root_a, service_root.resolve())
                self.assertIn("anonymous_spaces", str(root_a))
                self.assertNotIn(str(service_root), str(root_a))
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_downstream_client_id_without_workspace_does_not_use_gateway_env_root(self):
        import src.gateway_agent_planner as planner

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            old_strict = os.environ.get("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN")
            service_root = pathlib.Path(td) / "gateway-service-root"
            service_root.mkdir()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = "1"
            planner._STORE = None
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                cfg["gateway"]["agent_planner_strict_every_turn"] = True
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                client = FakeClient([
                    {
                        "choices": [{
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }]
                    }
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {"model": "m", "messages": [{"role": "user", "content": "hello"}]},
                    client,
                    client_id="downstream-key-client",
                )

                session_key = (final.get("gateway_context") or {}).get("agent_planner", {}).get("session_key", "")
                self.assertIn("anonymous_spaces", session_key)
                self.assertNotIn(str(service_root.resolve()), session_key)
                upstream_body = client.requests[0][1]
                self.assertNotIn("client_id", upstream_body)
                self.assertNotIn("downstream-key-client", json.dumps(upstream_body, ensure_ascii=False))
            finally:
                planner._STORE = None
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if old_strict is None:
                    os.environ.pop("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN", None)
                else:
                    os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = old_strict

    def test_downstream_client_id_overrides_body_client_id_for_runtime_scope(self):
        import src.gateway_agent_planner as planner
        from src.gateway_tool_runtime import _request_scope_body, _request_workspace_root

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            old_strict = os.environ.get("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN")
            service_root = pathlib.Path(td) / "gateway-service-root"
            service_root.mkdir()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = "1"
            planner._STORE = None
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                cfg["gateway"]["agent_planner_strict_every_turn"] = True
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                request_body = {
                    "model": "m",
                    "client_id": "spoofed-client",
                    "metadata": {"session_id": "shared-session"},
                    "messages": [{"role": "user", "content": "hello"}],
                }
                scoped = _request_scope_body(request_body, "authenticated-client")
                self.assertTrue(scoped["client_id"].startswith("client:"))
                self.assertNotEqual(scoped["client_id"], "authenticated-client")
                self.assertNotEqual(scoped["client_id"], "spoofed-client")
                self.assertEqual(request_body["client_id"], "spoofed-client")
                authenticated_root = _request_workspace_root(scoped)
                other_root = _request_workspace_root(
                    _request_scope_body({"metadata": {"session_id": "shared-session"}}, "other-client")
                )
                self.assertNotEqual(authenticated_root, other_root)
                self.assertNotIn(str(service_root.resolve()), str(authenticated_root))

                client = FakeClient([
                    {
                        "choices": [{
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }]
                    }
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    request_body,
                    client,
                    client_id="authenticated-client",
                )

                session_key = (final.get("gateway_context") or {}).get("agent_planner", {}).get("session_key", "")
                self.assertIn(f"tenant:{scoped['client_id']}", session_key)
                self.assertNotIn("tenant:spoofed-client", session_key)
                upstream_body = client.requests[0][1]
                self.assertNotIn("client_id", upstream_body)
                self.assertNotIn("spoofed-client", json.dumps(upstream_body, ensure_ascii=False))
                self.assertNotIn("authenticated-client", json.dumps(upstream_body, ensure_ascii=False))
            finally:
                planner._STORE = None
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if old_strict is None:
                    os.environ.pop("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN", None)
                else:
                    os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = old_strict

    def test_streaming_downstream_client_id_without_workspace_does_not_use_gateway_env_root(self):
        import io
        from src.gateway_streaming import run_streaming_orchestration

        class Handler:
            def __init__(self):
                self.wfile = io.BytesIO()
                self.headers = {}
                self.status = None
                self.sent_headers = []

            def send_response(self, status):
                self.status = status

            def send_header(self, key, value):
                self.sent_headers.append((key, value))

            def end_headers(self):
                pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            service_root = pathlib.Path(td) / "gateway-service-root"
            service_root.mkdir()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                gateway.save_config(cfg)

                captured = {}

                def fake_scoped(handler, path, body, **kwargs):
                    from src.gateway_builtin_tools import _workspace_root

                    captured["workspace_root"] = str(_workspace_root())
                    captured["body_has_client_id"] = "client_id" in body

                with patch("src.gateway_streaming._run_streaming_orchestration_scoped", side_effect=fake_scoped):
                    run_streaming_orchestration(
                        Handler(),
                        "/v1/chat/completions",
                        {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hello"}]},
                        client_id="stream-client",
                    )

                self.assertIn("anonymous_spaces", captured["workspace_root"])
                self.assertNotIn(str(service_root.resolve()), captured["workspace_root"])
                self.assertFalse(captured["body_has_client_id"])
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime

    def test_gateway_owned_public_helpers_scope_downstream_client_id_without_workspace(self):
        import src.gateway_agent_planner as planner
        from src.gateway_agent_planner import list_runtime_events
        from src.gateway_tool_runtime import execute_direct_tool_call, record_gateway_public_endpoint, token_count_response

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            service_root = pathlib.Path(td) / "gateway-service-root"
            service_root.mkdir()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            planner._STORE = None
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                gateway.save_config(cfg)

                calc = execute_direct_tool_call(
                    {"tool": "calculator", "arguments": {"expression": "2+3"}},
                    path="/v1/tools/call",
                    client_id="public-client",
                )
                self.assertTrue(calc["success"])
                token_count_response(
                    {"model": "m", "messages": [{"role": "user", "content": "hello"}]},
                    path="/v1/messages/count_tokens",
                    client_id="public-client",
                )
                record_gateway_public_endpoint(
                    "/v1/assistants",
                    {"model": "m"},
                    resource="assistants",
                    action="create",
                    response={"id": "asst_test", "object": "assistant"},
                    client_id="public-client",
                )

                events = list_runtime_events(20)
                self.assertGreaterEqual(len(events), 5)
                for event in events:
                    workspace_key = event.get("workspace_key", "")
                    self.assertIn("anonymous_spaces", workspace_key)
                    self.assertNotIn(str(service_root.resolve()), workspace_key)
            finally:
                planner._STORE = None
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime

    def test_tool_result_cache_keys_include_runtime_scope_not_only_workspace(self):
        import src.gateway_cache as cache_mod

        class SpyToolCache:
            def __init__(self):
                self.get_args = []
                self.put_args = []

            def is_cacheable(self, tool_name):
                return tool_name == "JsonQuery"

            def get(self, tool_name, arguments):
                self.get_args.append(dict(arguments))
                return None

            def put(self, tool_name, arguments, result):
                self.put_args.append(dict(arguments))

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_get_tool_result_cache = cache_mod.get_tool_result_cache
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            spy = SpyToolCache()
            cache_mod.get_tool_result_cache = lambda: spy
            try:
                workspace = pathlib.Path(td) / "shared-client-workspace"
                workspace.mkdir()
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                gateway.save_config(gateway._default_config())
                body = {
                    "workspace_root": str(workspace),
                    "session_id": "cache-session",
                    "tool": "JsonQuery",
                    "arguments": {"data": {"answer": 42}, "query": "answer"},
                }

                result_a = execute_direct_tool_call(body, client_id="cache-client-a")
                result_b = execute_direct_tool_call(body, client_id="cache-client-b")

                self.assertTrue(result_a["success"])
                self.assertTrue(result_b["success"])
                self.assertEqual(len(spy.get_args), 2)
                self.assertEqual(spy.get_args[0]["__gateway_workspace_cache_key"], spy.get_args[1]["__gateway_workspace_cache_key"])
                self.assertIn("__gateway_runtime_cache_key", spy.get_args[0])
                self.assertIn("__gateway_runtime_cache_key", spy.get_args[1])
                self.assertNotEqual(
                    spy.get_args[0]["__gateway_runtime_cache_key"],
                    spy.get_args[1]["__gateway_runtime_cache_key"],
                )
                self.assertEqual([args["__gateway_runtime_cache_key"] for args in spy.put_args], [args["__gateway_runtime_cache_key"] for args in spy.get_args])
            finally:
                cache_mod.get_tool_result_cache = old_get_tool_result_cache
                gateway.CONFIG_PATH = old_config
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_gateway_owned_post_routes_pass_downstream_key_as_client_id(self):
        captured = {"token": [], "direct": [], "public": []}

        def fake_token(body, *, path="/v1/messages/count_tokens", client_id=None):
            captured["token"].append((path, client_id))
            return {"input_tokens": 1}

        def fake_direct(body, *, path="/tools/call", client_id=None):
            captured["direct"].append((path, client_id))
            return {"success": True, "content": "ok"}

        def fake_public(path, body, **kwargs):
            captured["public"].append((path, kwargs.get("client_id"), kwargs.get("resource")))

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = gateway._default_config()
                cfg["downstream_keys"] = [{
                    "name": "public-client",
                    "key_hash": gateway._hash_secret("public-key"),
                    "prefix": "public-k",
                    "enabled": True,
                    "protocols": ["models", "messages", "direct_tools"],
                }]
                gateway.save_config(cfg)

                def post(path, payload):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_address[1]}{path}",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"content-type": "application/json", "authorization": "Bearer public-key"},
                        method="POST",
                    )
                    return urllib.request.urlopen(req, timeout=5).read()

                with patch("src.gateway_tool_runtime.token_count_response", side_effect=fake_token), \
                     patch("src.gateway_tool_runtime.execute_direct_tool_call", side_effect=fake_direct), \
                     patch("src.gateway_tool_runtime.record_gateway_public_endpoint", side_effect=fake_public):
                    post("/v1/messages/count_tokens", {"model": "m", "messages": []})
                    post("/v1/tools/call", {"tool": "calculator", "arguments": {"expression": "1+1"}})
                    post("/v1/assistants", {"model": "m"})

                client_id = gateway.load_config()["downstream_keys"][0]["id"]
                self.assertEqual(captured["token"], [("/v1/messages/count_tokens", client_id)])
                self.assertEqual(captured["direct"], [("/v1/tools/call", client_id)])
                self.assertEqual(captured["public"], [("/v1/assistants", client_id, "assistants")])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_remote_identity_memory_without_scope_does_not_use_gateway_env_root(self):
        from src.gateway_context import _inject_recalled_memories, _remember_conversation_turn, _sqlite_tail_memories

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            gateway.SQLITE_READY = False
            service_root = pathlib.Path(td) / "gateway-service-root"
            service_root.mkdir()
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                cfg["context"]["memory_enabled"] = True
                cfg["context"]["memory_summary_max_chars"] = 500
                cfg["context"]["memory_recall_limit"] = 5
                gateway.save_config(cfg)

                metadata = {
                    "session_id": "remote-memory-session",
                    "user_id": json.dumps({"user_id": "remote-memory-user"}),
                }
                body = {
                    "model": "m",
                    "metadata": metadata,
                    "messages": [{"role": "user", "content": "Remember cloud marker CLOUD-MEM-BOUNDARY"}],
                }
                _remember_conversation_turn(
                    "/v1/chat/completions",
                    body,
                    {"choices": [{"message": {"role": "assistant", "content": "Recorded CLOUD-MEM-BOUNDARY"}}]},
                )

                memories = _sqlite_tail_memories(10)
                self.assertEqual(len(memories), 1)
                workspace_key = memories[0]["workspace_key"]
                self.assertNotEqual(workspace_key, str(service_root.resolve()))
                self.assertIn("anonymous_spaces", workspace_key)
                self.assertNotIn(str(service_root.resolve()), workspace_key)

                recalled = _inject_recalled_memories(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "metadata": metadata,
                        "messages": [{"role": "user", "content": "Which cloud marker did we record?"}],
                    },
                )
                self.assertIn("CLOUD-MEM-BOUNDARY", json.dumps(recalled, ensure_ascii=False))
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.SQLITE_READY = old_ready
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_remote_anonymous_workspace_accepts_metadata_tenant_alias(self):
        from src.gateway_tool_runtime import _request_workspace_root

        body_a = {"metadata": {"tenant": "tenant-alias-a", "session_id": "shared-session"}}
        body_b = {"metadata": {"tenant": "tenant-alias-b", "session_id": "shared-session"}}
        root_a = _request_workspace_root(body_a)
        root_a_again = _request_workspace_root(body_a)
        root_b = _request_workspace_root(body_b)
        self.assertEqual(root_a, root_a_again)
        self.assertNotEqual(root_a, root_b)

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
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = True
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

    def test_memory_tool_scopes_by_authenticated_client_id_and_blocks_global_listing(self):
        from src.gateway_context import _sqlite_tail_memories

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "gateway.sqlite3")
            gateway.SQLITE_READY = False
            try:
                project_root = pathlib.Path(td) / "shared-downstream-project"
                other_root = pathlib.Path(td) / "other-downstream-project"
                project_root.mkdir()
                other_root.mkdir()
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                cfg = gateway._default_config()
                gateway.save_config(cfg)

                write_a = execute_direct_tool_call(
                    {
                        "workspace_root": str(project_root),
                        "client_id": "spoofed-body-client-b",
                        "tool": "SaveMemory",
                        "arguments": {"action": "write", "summary": "CLIENT-A-MEMORY-OK", "keywords": ["client-a"]},
                    },
                    client_id="auth-client-a",
                )
                self.assertTrue(write_a["success"])
                write_b = execute_direct_tool_call(
                    {
                        "workspace_root": str(project_root),
                        "client_id": "spoofed-body-client-a",
                        "tool": "SaveMemory",
                        "arguments": {"action": "write", "summary": "CLIENT-B-MEMORY-SECRET", "keywords": ["client-b"]},
                    },
                    client_id="auth-client-b",
                )
                self.assertTrue(write_b["success"])
                execute_direct_tool_call(
                    {
                        "workspace_root": str(other_root),
                        "tool": "SaveMemory",
                        "arguments": {"action": "write", "summary": "OTHER-WORKSPACE-MEMORY-SECRET", "keywords": ["other"]},
                    },
                    client_id="auth-client-b",
                )

                recalled_a = execute_direct_tool_call(
                    {"workspace_root": str(project_root), "tool": "RecallMemory", "arguments": {"action": "list", "limit": 10}},
                    client_id="auth-client-a",
                )
                self.assertTrue(recalled_a["success"])
                self.assertIn("CLIENT-A-MEMORY-OK", recalled_a["content"])
                self.assertNotIn("CLIENT-B-MEMORY-SECRET", recalled_a["content"])
                self.assertNotIn("OTHER-WORKSPACE-MEMORY-SECRET", recalled_a["content"])

                recalled_b = execute_direct_tool_call(
                    {"workspace_root": str(project_root), "tool": "RecallMemory", "arguments": {"action": "list", "limit": 10}},
                    client_id="auth-client-b",
                )
                self.assertTrue(recalled_b["success"])
                self.assertIn("CLIENT-B-MEMORY-SECRET", recalled_b["content"])
                self.assertNotIn("CLIENT-A-MEMORY-OK", recalled_b["content"])

                global_list = execute_direct_tool_call(
                    {
                        "workspace_root": str(project_root),
                        "tool": "RecallMemory",
                        "arguments": {"action": "list", "include_all_workspaces": True, "limit": 10},
                    },
                    client_id="auth-client-a",
                )
                self.assertFalse(global_list["success"])
                self.assertEqual(global_list["failure_type"], "permission_denied")
                self.assertNotIn("CLIENT-B-MEMORY-SECRET", global_list["content"])
                self.assertNotIn("OTHER-WORKSPACE-MEMORY-SECRET", global_list["content"])

                raw_memories = _sqlite_tail_memories(10)
                self.assertEqual(len(raw_memories), 3)
                self.assertNotIn("anonymous", {m["tenant_key"] for m in raw_memories})
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
                cfg["gateway"]["allow_private_network_tools"] = True
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
        from src import gateway_computer_use as cu

        old_open_provider = cu._open_image_provider_url
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
            cu._open_image_provider_url = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network unavailable"))
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
            cu._open_image_provider_url = old_open_provider
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

    def test_image_generation_private_provider_url_requires_admin_opt_in(self):
        from src import gateway_computer_use as cu

        old_open_provider = cu._open_image_provider_url
        old_openai = os.environ.get("OPENAI_API_KEY")
        old_image_key = os.environ.get("IMAGE_GEN_API_KEY")
        old_base = os.environ.get("IMAGE_GEN_BASE_URL")
        old_hf = os.environ.get("HF_TOKEN")
        old_hf2 = os.environ.get("HUGGINGFACE_TOKEN")
        try:
            os.environ["OPENAI_API_KEY"] = "test-key"
            os.environ["IMAGE_GEN_BASE_URL"] = "http://127.0.0.1:9"
            os.environ.pop("IMAGE_GEN_API_KEY", None)
            os.environ.pop("HF_TOKEN", None)
            os.environ.pop("HUGGINGFACE_TOKEN", None)

            def fake_open(req, *, timeout):
                if "image.pollinations.ai" in getattr(req, "full_url", ""):
                    raise RuntimeError("pollinations disabled")
                return old_open_provider(req, timeout=timeout)

            cu._open_image_provider_url = fake_open
            payload = json.loads(cu._tool_image_generation({"prompt": "draw a gateway"}))
            self.assertFalse(payload["ok"])
            self.assertTrue(any("allow_private_network" in item for item in payload.get("provider_errors", [])))
        finally:
            cu._open_image_provider_url = old_open_provider
            for key, value in {
                "OPENAI_API_KEY": old_openai,
                "IMAGE_GEN_API_KEY": old_image_key,
                "IMAGE_GEN_BASE_URL": old_base,
                "HF_TOKEN": old_hf,
                "HUGGINGFACE_TOKEN": old_hf2,
            }.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_direct_image_generation_is_gateway_owned_provider_tool(self):
        from src import gateway_computer_use as cu

        old_open_provider = cu._open_image_provider_url
        old_openai = os.environ.get("OPENAI_API_KEY")
        old_image_key = os.environ.get("IMAGE_GEN_API_KEY")
        old_hf = os.environ.get("HF_TOKEN")
        old_hf2 = os.environ.get("HUGGINGFACE_TOKEN")
        try:
            cu._open_image_provider_url = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network unavailable"))
            for key in ("OPENAI_API_KEY", "IMAGE_GEN_API_KEY", "HF_TOKEN", "HUGGINGFACE_TOKEN"):
                os.environ.pop(key, None)
            result = execute_direct_tool_call(
                {"tool": "image_generation", "arguments": {"prompt": "draw a cloud gateway"}, "call_id": "direct_image_gateway_owned"}
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["failure_type"], "connector_required")
            self.assertIn("No real image generation provider", result["content"])
        finally:
            cu._open_image_provider_url = old_open_provider
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
        old_config = gateway.CONFIG_PATH
        os.environ["UPSTREAM_PROTOCOL"] = "openai_chat"
        try:
            with tempfile.TemporaryDirectory() as td:
                gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
                cfg = gateway._default_config()
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["tools_enabled"] = "native"
                cfg["upstream"]["capabilities"]["supports_tools"] = True
                cfg["upstream"]["capabilities"]["supports_function_calls"] = True
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
            gateway.CONFIG_PATH = old_config
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
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
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
            self.assertEqual(_configured_max_tool_rounds({}), 10)

            # canonical default 10 when config has no max_tool_rounds
            with patch("src.gateway_config._gateway_config", return_value={}):
                self.assertEqual(_configured_max_tool_rounds(None), 10)

            # invalid env var falls back to default
            os.environ["GATEWAY_MAX_TOOL_ROUNDS"] = "not-a-number"
            self.assertEqual(_configured_max_tool_rounds({}), 10)

            # invalid config value falls back to default
            os.environ.pop("GATEWAY_MAX_TOOL_ROUNDS", None)
            self.assertEqual(_configured_max_tool_rounds({"max_tool_rounds": "bad"}), 10)
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
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
                cfg["gateway"]["text_tool_call_fallback_enabled"] = True
                cfg["gateway"]["delegate_tools_to_downstream"] = False
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = True
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
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
                cfg["upstream"]["tools_enabled"] = "auto"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                cfg["gateway"]["text_tool_call_fallback_enabled"] = True
                cfg["gateway"]["delegate_tools_to_downstream"] = False
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = True
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

    def test_delegate_tools_to_downstream_false_does_not_enable_cloud_local_user_side_execution(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_exec = os.environ.get("GATEWAY_EXECUTE_USER_SIDE_TOOLS")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ.pop("GATEWAY_EXECUTE_USER_SIDE_TOOLS", None)
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                cfg["gateway"]["text_tool_call_fallback_enabled"] = True
                cfg["gateway"]["local_planner_enabled"] = False
                cfg["gateway"]["delegate_tools_to_downstream"] = False
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = False
                gateway.save_config(cfg)
                pathlib.Path(td, "cloud_only.py").write_text("print('cloud')\n", encoding="utf-8")
                client = FakeClient([
                    {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "<function=Glob>\n<parameter=pattern>/*.py",
                            },
                            "finish_reason": "stop",
                        }]
                    }
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "Please continue."}],
                    },
                    client,
                )
                choice = final["choices"][0]
                self.assertEqual(choice.get("finish_reason"), "tool_calls")
                tool_calls = choice["message"].get("tool_calls") or []
                self.assertEqual(tool_calls[0]["function"]["name"], "Glob")
                self.assertIn("*.py", tool_calls[0]["function"]["arguments"])
                self.assertEqual(len(client.requests), 1)
                self.assertNotIn("cloud_only.py", json.dumps(final))
                self.assertNotIn("gateway_local_tool_fallback", json.dumps(final))
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_exec is None:
                    os.environ.pop("GATEWAY_EXECUTE_USER_SIDE_TOOLS", None)
                else:
                    os.environ["GATEWAY_EXECUTE_USER_SIDE_TOOLS"] = old_exec

    def test_text_tool_adapter_compacts_huge_claude_code_payload_before_upstream(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
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
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
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
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
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
                            "allow_private_network": True,
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
                    {"choices": [{"message": {"role": "assistant", "content": "Shanghai weather is sunny, 21C."}, "finish_reason": "stop"}]},
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "Weather in Shanghai?"}],
                    },
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "Shanghai weather is sunny, 21C.")
                self.assertEqual(WeatherHandler.seen[0]["city"], ["Shanghai"])
                self.assertEqual(len(client.requests), 1)
                self.assertIn("temp_c", client.requests[0][1]["messages"][-1]["content"])
                self.assertNotIn("gateway_context", client.requests[0][1])
                self.assertNotIn("gateway_agent_planner", client.requests[0][1])
                self.assertEqual(final["gateway_context"]["agent_planner"]["workflow"], "gateway_owned_tool")
                self.assertNotIn("tools", client.requests[0][1])
                self.assertNotIn("tool_choice", client.requests[0][1])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_gateway_owned_builtin_calculator_preexecutes_without_request_tools(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)
                client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "The result is 42."}, "finish_reason": "stop"}]},
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "Calculate 6*7 for me"}],
                    },
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "The result is 42.")
                self.assertEqual(len(client.requests), 1)
                sent = client.requests[0][1]
                self.assertIn("42", sent["messages"][-1]["content"])
                self.assertNotIn("gateway_context", sent)
                self.assertNotIn("gateway_agent_planner", sent)
                self.assertEqual(final["gateway_context"]["agent_planner"]["tool"], "calculator")
                self.assertNotIn("tools", sent)
                self.assertNotIn("tool_choice", sent)
            finally:
                gateway.CONFIG_PATH = old_config

    def test_gateway_owned_multiple_builtin_tools_preexecute_without_request_tools(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)
                client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "The result is 42 and the time was included."}, "finish_reason": "stop"}]},
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "Calculate 6*7 and tell current time"}],
                    },
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "The result is 42 and the time was included.")
                self.assertEqual(len(client.requests), 1)
                sent = client.requests[0][1]
                tool_contents = "\n".join(
                    str(message.get("content") or "")
                    for message in sent["messages"]
                    if isinstance(message, dict) and message.get("role") == "tool"
                )
                self.assertIn("42", tool_contents)
                self.assertIn("+00:00", tool_contents)
                self.assertNotIn("gateway_context", sent)
                self.assertNotIn("gateway_agent_planner", sent)
                planner_ctx = final["gateway_context"]["agent_planner"]
                self.assertEqual(planner_ctx["workflow"], "gateway_owned_tool")
                self.assertEqual(set(planner_ctx["tools"]), {"calculator", "get_current_time"})
                self.assertIn(planner_ctx["tool"], planner_ctx["tools"])
                self.assertNotIn("tools", sent)
                self.assertNotIn("tool_choice", sent)
            finally:
                gateway.CONFIG_PATH = old_config

    def test_gateway_owned_preexecute_records_runtime_events_by_remote_scope(self):
        from src.gateway_agent_planner import list_runtime_events

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            planner = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)
                client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "The result is 42."}, "finish_reason": "stop"}]},
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "metadata": {
                            "session_id": "remote-calc-session",
                            "user_id": json.dumps({"user_id": "remote-calc-user"}),
                        },
                        "messages": [{"role": "user", "content": "Calculate 6*7 for me"}],
                    },
                    client,
                )
                self.assertEqual(final["gateway_context"]["agent_planner"]["workflow"], "gateway_owned_tool")
                events = list_runtime_events(10, tenant_contains="remote-calc-user", workflow="gateway_owned_tool")
                event_types = [event["event_type"] for event in events]
                self.assertIn("gateway_tool_execute", event_types)
                self.assertIn("gateway_tool_result", event_types)
                result_event = next(event for event in events if event["event_type"] == "gateway_tool_result")
                self.assertEqual(result_event["metadata"]["tool"], "calculator")
                self.assertTrue(result_event["metadata"]["success"])
                self.assertIn("remote-calc-session", result_event["session_key"])
            finally:
                gateway.CONFIG_PATH = old_config
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if planner is not None:
                    planner._STORE = None

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
                self.assertIn("最终答案改写器", client.requests[3][1]["messages"][-1]["content"])
                self.assertIn("只返回用户可以直接使用的最终答案", client.requests[3][1]["messages"][-1]["content"])
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
                alias_page = urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_address[1]}/config",
                        headers={"authorization": f"Basic {token}"},
                    ),
                    timeout=5,
                ).read().decode("utf-8")
                self.assertIn("Gateway Control Center", alias_page)
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

    def test_admin_skill_delete_rejects_path_traversal_and_cross_origin(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_cwd = os.getcwd()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.chdir(td)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                gateway.save_config(gateway._default_config())
                token = base64.b64encode(b"admin:admin").decode("ascii")
                base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
                victim = pathlib.Path(td) / "outside-victim"
                victim.mkdir()
                (victim / "marker.txt").write_text("do-not-delete", encoding="utf-8")

                traversal_body = json.dumps({"name": "../outside-victim"}).encode("utf-8")
                req = urllib.request.Request(
                    f"{base_url}/admin/skill-delete.json",
                    data=traversal_body,
                    headers={"authorization": f"Basic {token}", "content-type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(err.exception.code, 400)
                self.assertTrue(victim.is_dir())
                self.assertTrue((victim / "marker.txt").exists())

                skill_dir = pathlib.Path(td) / "skills" / "safe-skill"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text("safe", encoding="utf-8")
                cross_origin_body = json.dumps({"name": "safe-skill"}).encode("utf-8")
                req = urllib.request.Request(
                    f"{base_url}/admin/skill-delete.json",
                    data=cross_origin_body,
                    headers={
                        "authorization": f"Basic {token}",
                        "content-type": "application/json",
                        "Origin": "https://evil.example",
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(err.exception.code, 403)
                self.assertTrue(skill_dir.is_dir())
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                os.chdir(old_cwd)
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
                saved = gateway.load_config()
                client_ids = {item["name"]: item["id"] for item in saved["downstream_keys"]}
                self.assertEqual(gateway._check_downstream_key(DummyHandler("/v1/models", "chat-only-key")), client_ids["chat-only"])
                self.assertEqual(gateway._check_downstream_key(DummyHandler("/v1/tools/call", "tools-only-key")), client_ids["tools-only"])
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

    def test_streaming_post_passes_stable_downstream_key_id_as_client_id(self):
        captured = {}

        def fake_stream(handler, path, body, client_id=None):
            captured["path"] = path
            captured["client_id"] = client_id
            captured["stream"] = body.get("stream")
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(b"data: [DONE]\n\n")

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
                        "name": "stream-client",
                        "key_hash": gateway._hash_secret("stream-key"),
                        "prefix": "stream-k",
                        "enabled": True,
                        "protocols": ["chat_completions"],
                    }
                ]
                gateway.save_config(cfg)
                body = json.dumps(
                    {
                        "model": "m",
                        "stream": True,
                        "messages": [{"role": "user", "content": "Calculate 6*7"}],
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
                    data=body,
                    headers={"content-type": "application/json", "authorization": "Bearer stream-key"},
                    method="POST",
                )
                with patch("src.gateway_streaming.run_streaming_orchestration", side_effect=fake_stream):
                    urllib.request.urlopen(req, timeout=5).read()
                self.assertEqual(captured["path"], "/v1/chat/completions")
                self.assertTrue(captured["stream"])
                client_id = gateway.load_config()["downstream_keys"][0]["id"]
                self.assertEqual(captured["client_id"], client_id)
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

    def test_fanout_preserves_source_order_and_reports_truncation(self):
        import time

        class OutOfOrderClient:
            def __init__(self):
                self.synthesis_prompt = ""

            def forward(self, path, body):
                prompt = body["messages"][-1]["content"]
                if prompt.startswith("片段 1/2"):
                    time.sleep(0.05)
                    return {"choices": [{"message": {"content": "first partial"}}]}
                if prompt.startswith("片段 2/2"):
                    return {"choices": [{"message": {"content": "second partial"}}]}
                self.synthesis_prompt = prompt
                return {"choices": [{"message": {"content": "final"}}]}

        client = OutOfOrderClient()
        result = gateway._run_context_fanout(
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "x" * 300}]},
            client,
            {"context": {
                "fanout_enabled": True,
                "max_input_tokens": 1,
                "fanout_chunk_tokens": 10,
                "fanout_max_chunks": 2,
                "fanout_max_workers": 2,
                "quality_review_enabled": False,
            }},
        )
        self.assertLess(client.synthesis_prompt.index("first partial"), client.synthesis_prompt.index("second partial"))
        self.assertTrue(result["gateway_context"]["truncated"])
        self.assertGreater(result["gateway_context"]["omitted_source_chars"], 0)
        self.assertEqual(result["gateway_context"]["successful_chunks"], 2)

    def test_fanout_reports_failed_chunk_and_review_prompt_demands_final_answer(self):
        class PartialFailureClient:
            def forward(self, path, body):
                prompt = body["messages"][-1]["content"]
                if prompt.startswith("片段 1/2"):
                    return {"choices": [{"message": {"content": "surviving partial"}}]}
                if prompt.startswith("片段 2/2"):
                    raise RuntimeError("chunk failed")
                return {"choices": [{"message": {"content": "final from partial"}}]}

        result = gateway._run_context_fanout(
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "x" * 100}]},
            PartialFailureClient(),
            {"context": {
                "fanout_enabled": True,
                "max_input_tokens": 1,
                "fanout_chunk_tokens": 10,
                "fanout_max_chunks": 2,
                "fanout_max_workers": 2,
                "quality_review_enabled": False,
            }},
        )
        self.assertEqual(result["gateway_context"]["failed_chunks"], [2])
        self.assertEqual(result["gateway_context"]["successful_chunks"], 1)
        review = gateway._make_quality_review_prompt("question", "draft")
        self.assertIn("只返回用户可以直接使用的最终答案", review)
        self.assertIn("不要输出审查过程", review)

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
        old_config = gateway.CONFIG_PATH
        os.environ["UPSTREAM_PROTOCOL"] = "openai_chat"
        try:
            with tempfile.TemporaryDirectory() as td:
                gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
                cfg = gateway._default_config()
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["tools_enabled"] = "native"
                cfg["upstream"]["capabilities"]["supports_tools"] = True
                cfg["upstream"]["capabilities"]["supports_function_calls"] = True
                gateway.save_config(cfg)
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
            gateway.CONFIG_PATH = old_config
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
        old_config = gateway.CONFIG_PATH
        os.environ["UPSTREAM_PROTOCOL"] = "openai_chat"
        with tempfile.TemporaryDirectory() as td:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["upstream"]["tools_enabled"] = "native"
            cfg["upstream"]["capabilities"]["supports_tools"] = True
            cfg["upstream"]["capabilities"]["supports_function_calls"] = True
            gateway.save_config(cfg)
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
        gateway.CONFIG_PATH = old_config
        if old_protocol:
            os.environ["UPSTREAM_PROTOCOL"] = old_protocol
        else:
            os.environ.pop("UPSTREAM_PROTOCOL", None)

    def test_claude_messages_orchestrates_calc_alias_expr_until_final(self):
        old_config = gateway.CONFIG_PATH
        with tempfile.TemporaryDirectory() as td:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["upstream"]["tools_enabled"] = "native"
            cfg["upstream"]["capabilities"]["supports_tools"] = True
            cfg["upstream"]["capabilities"]["supports_function_calls"] = True
            gateway.save_config(cfg)
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
        gateway.CONFIG_PATH = old_config

    def test_orchestrates_messages_until_final(self):
        # Set upstream to OpenAI Chat to match test expectation
        old_protocol = os.environ.get("UPSTREAM_PROTOCOL")
        old_config = gateway.CONFIG_PATH
        os.environ["UPSTREAM_PROTOCOL"] = "openai_chat"
        try:
            with tempfile.TemporaryDirectory() as td:
                gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
                cfg = gateway._default_config()
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["tools_enabled"] = "native"
                cfg["upstream"]["capabilities"]["supports_tools"] = True
                cfg["upstream"]["capabilities"]["supports_function_calls"] = True
                gateway.save_config(cfg)
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
            gateway.CONFIG_PATH = old_config
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
                cfg["upstream"]["tools_enabled"] = "native"
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
                merged = _merge_builtin_tools(
                    "/v1/chat/completions",
                    {
                        "gateway_context": {"client_can_handle_implicit_tools": True},
                        "messages": [{"role": "user", "content": "inspect the project files"}],
                    },
                )
                names = [
                    t.get("function", {}).get("name")
                    for t in merged.get("tools", [])
                    if isinstance(t, dict) and isinstance(t.get("function"), dict)
                ]
                self.assertIn(public_name, names)
                self.assertIn(legacy_name, names)
            finally:
                gateway._mcp_close_sessions()
                gateway.MCP_TOOL_CATALOG_CACHE.clear()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_mcp_service_file_arguments_require_admin_opt_in(self):
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
    elif method == "tools/call":
        path = msg.get("params", {}).get("arguments", {}).get("path", "")
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"content":[{"type":"text","text":"path:" + path}]}})
    else:
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{}})
'''
        with tempfile.TemporaryDirectory() as td:
            script_path = pathlib.Path(td) / "fake_file_mcp.py"
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
                            "name": "files",
                            "command": sys.executable,
                            "args": [str(script_path)],
                            "cwd": td,
                            "enabled": True,
                        }
                    ],
                }
                gateway.save_config(cfg)
                public_name = _mcp_public_name("files", "read_file")
                blocked = _execute_tool_call(ToolCall("mcp-file-block", public_name, {"path": "/etc/passwd"}, {}))
                self.assertFalse(blocked.success)
                self.assertEqual(blocked.failure_type, "invalid_input")
                self.assertIn("allow_service_file_arguments", blocked.content)
                self.assertEqual(len(gateway.MCP_SESSIONS), 0)
                blocked_generic = _execute_tool_call(
                    ToolCall(
                        "mcp-file-generic-block",
                        "mcp_call_tool",
                        {"server": "files", "name": "read_file", "arguments": {"path": "/etc/passwd"}},
                        {},
                    )
                )
                self.assertFalse(blocked_generic.success)
                self.assertEqual(blocked_generic.failure_type, "invalid_input")
                self.assertIn("allow_service_file_arguments", blocked_generic.content)
                self.assertEqual(len(gateway.MCP_SESSIONS), 0)

                cfg["mcp"]["servers"][0]["allow_service_file_arguments"] = True
                gateway.save_config(cfg)
                allowed = _execute_tool_call(ToolCall("mcp-file-allow", public_name, {"path": "/etc/passwd"}, {}))
                self.assertTrue(allowed.success)
                self.assertEqual(allowed.content, "path:/etc/passwd")
            finally:
                gateway._mcp_close_sessions()
                gateway.MCP_TOOL_CATALOG_CACHE.clear()
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_configured_mcp_tool_preexecutes_without_request_tools(self):
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
            script_path = pathlib.Path(td) / "fake_mcp_preexec.py"
            script_path.write_text(script, encoding="utf-8")
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                cfg["mcp"] = {
                    "enabled": True,
                    "servers": [{
                        "name": "test",
                        "command": sys.executable,
                        "args": [str(script_path)],
                        "cwd": td,
                        "enabled": True,
                    }],
                }
                gateway.save_config(cfg)
                client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "MCP final ok."}, "finish_reason": "stop"}]},
                ])
                final = run_tool_orchestration(
                    "/v1/chat/completions",
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "Echo via MCP value ok"}],
                    },
                    client,
                )
                self.assertEqual(final["choices"][0]["message"]["content"], "MCP final ok.")
                self.assertEqual(len(client.requests), 1)
                sent = client.requests[0][1]
                self.assertIn("mcp:Echo via MCP value ok", sent["messages"][-1]["content"])
                self.assertNotIn("gateway_context", sent)
                self.assertNotIn("gateway_agent_planner", sent)
                self.assertEqual(final["gateway_context"]["agent_planner"]["workflow"], "gateway_owned_tool")
                self.assertNotIn("tools", sent)
            finally:
                gateway._mcp_close_sessions()
                gateway.MCP_TOOL_CATALOG_CACHE.clear()
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
                            "allow_service_file_arguments": True,
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

    def test_admin_agent_planner_endpoint_lists_runtime_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            httpd = None
            thread = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                planner._store().save(
                    "/v1/messages:workspace:tenant:other-tenant:session:other-session",
                    {
                        "session_key": "/v1/messages:workspace:tenant:other-tenant:session:other-session",
                        "workspace_key": "/client/other-workspace",
                        "workflow": "fix_test",
                        "current_step": "pytest",
                        "completed_steps": ["pytest"],
                        "evidence_count": 1,
                        "evidence_summary": "bounded evidence preview",
                    },
                )

                planned = run_tool_orchestration("/v1/messages", {
                    "model": "weak",
                    "metadata": {"session_id": "planner-admin-session", "user_id": json.dumps({"user_id": "planner-admin-user"})},
                    "messages": [
                        {"role": "system", "content": "Available skills:\n- codebase-onboarding"},
                        {"role": "user", "content": "分析这套项目"},
                    ],
                    "tools": [{
                        "name": "Skill",
                        "input_schema": {
                            "type": "object",
                            "properties": {"skill": {"type": "string"}, "args": {"type": "string"}},
                            "required": ["skill"],
                            "additionalProperties": False,
                        },
                    }],
                })
                self.assertEqual((planned.get("gateway_context") or {}).get("agent_planner", {}).get("workflow"), "project_analysis")

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                token = base64.b64encode(b"admin:admin").decode("ascii")
                base_url = f"http://127.0.0.1:{httpd.server_address[1]}/admin/agent-planner.json"

                unauth_req = urllib.request.Request(f"{base_url}?limit=10")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(unauth_req, timeout=5)
                self.assertEqual(err.exception.code, 401)

                def fetch(query: str):
                    req = urllib.request.Request(
                        f"{base_url}?{query}",
                        headers={"authorization": f"Basic {token}"},
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        self.assertEqual(resp.status, 200)
                        return json.loads(resp.read().decode("utf-8"))

                payload = fetch("limit=10")
                self.assertTrue(payload["sessions"])
                session = payload["sessions"][0]
                self.assertEqual(session["workflow"], "project_analysis")
                self.assertEqual(session["current_step"], "codebase_onboarding")
                self.assertIn("planner-admin-user", session["session_key"])
                self.assertIn("planner-admin-session", session["session_key"])

                filtered = fetch("limit=10&workflow=project_analysis&current_step=codebase_onboarding&session_contains=planner-admin-user")
                self.assertEqual(len(filtered["sessions"]), 1)
                self.assertEqual(filtered["sessions"][0]["workflow"], "project_analysis")
                self.assertEqual(filtered["filters"]["workflow"], "project_analysis")
                self.assertEqual(filtered["filters"]["current_step"], "codebase_onboarding")

                tenant_filtered = fetch("limit=10&tenant_contains=other-tenant&has_evidence=1")
                self.assertEqual(len(tenant_filtered["sessions"]), 1)
                self.assertEqual(tenant_filtered["sessions"][0]["workflow"], "fix_test")
                self.assertEqual(tenant_filtered["sessions"][0]["tenant_key"], "other-tenant")
                self.assertEqual(tenant_filtered["sessions"][0]["evidence_count"], 1)
                workspace_filtered = fetch("limit=10&workspace_contains=other-workspace")
                self.assertEqual(len(workspace_filtered["sessions"]), 1)
                self.assertEqual(workspace_filtered["sessions"][0]["workspace_key"], "/client/other-workspace")
                self.assertEqual(workspace_filtered["filters"]["workspace_contains"], "other-workspace")

                no_evidence = fetch("limit=10&has_evidence=0&session_contains=planner-admin-session")
                self.assertEqual(len(no_evidence["sessions"]), 1)
                self.assertEqual(no_evidence["sessions"][0]["workflow"], "project_analysis")
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                planner._STORE = None

    def test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope(self):
        from src.gateway_context import _sqlite_insert_memory

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(pathlib.Path(td) / "workspace")
            pathlib.Path(os.environ["GATEWAY_WORKSPACE_ROOT"]).mkdir()
            gateway.SQLITE_READY = False
            httpd = None
            thread = None
            planner = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                gateway.save_config(gateway._default_config())
                planner._store().save(
                    "/v1/messages:/client/runtime:tenant:user-runtime:session_id:session-runtime",
                    {
                        "tenant_key": "user-runtime",
                        "workspace_key": "/client/runtime",
                        "workflow": "project_analysis",
                        "current_step": "synthesis",
                        "completed_steps": ["Skill", "Read"],
                        "evidence_count": 3,
                        "evidence_summary": "runtime evidence",
                    },
                )
                planner._store().save(
                    "/v1/messages:/client/other-runtime:tenant:user-runtime:session_id:session-runtime-other-workspace",
                    {
                        "tenant_key": "user-runtime",
                        "workspace_key": "/client/other-runtime",
                        "workflow": "project_analysis",
                        "current_step": "synthesis",
                        "completed_steps": ["Skill"],
                        "evidence_count": 2,
                        "evidence_summary": "other workspace evidence",
                    },
                )
                _sqlite_insert_memory(
                    "tenant:user-runtime:session:session-runtime",
                    "/client/runtime",
                    "session_rollup",
                    "runtime rollup",
                    ["runtime"],
                    None,
                    5,
                )
                _sqlite_insert_memory(
                    "tenant:other-user:session:other-session",
                    "/client/other",
                    "session_rollup",
                    "other rollup",
                    ["other"],
                    None,
                    5,
                )

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                base_url = f"http://127.0.0.1:{httpd.server_address[1]}/admin/agent-runtime.json"
                token = base64.b64encode(b"admin:admin").decode("ascii")

                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(urllib.request.Request(f"{base_url}?limit=10"), timeout=5)
                self.assertEqual(err.exception.code, 401)

                req = urllib.request.Request(
                    f"{base_url}?limit=10&tenant_contains=user-runtime&workspace_contains=/client/runtime&session_contains=session-runtime&workflow=project_analysis&has_evidence=1&has_rollup=1",
                    headers={"authorization": f"Basic {token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))

                runtime = payload["runtime"]
                self.assertEqual(payload["filters"]["tenant_contains"], "user-runtime")
                self.assertEqual(payload["filters"]["workspace_contains"], "/client/runtime")
                self.assertEqual(payload["limit"], 10)
                self.assertEqual(runtime["agent_planner"]["session_count"], 1)
                self.assertEqual(runtime["agent_planner"]["sessions"][0]["workflow"], "project_analysis")
                self.assertEqual(runtime["agent_planner"]["sessions"][0]["tenant_key"], "user-runtime")
                self.assertEqual(runtime["agent_planner"]["sessions"][0]["workspace_key"], "/client/runtime")
                self.assertEqual(runtime["memory"]["memory_count"], 1)
                self.assertEqual(runtime["memory"]["rollup_count"], 1)
                self.assertEqual(runtime["memory"]["memories"][0]["summary"], "runtime rollup")
                self.assertGreaterEqual(runtime["events"]["event_count"], 1)
                self.assertEqual(runtime["events"]["items"][0]["event_type"], "planner_state")
                self.assertNotIn("other rollup", json.dumps(payload, ensure_ascii=False))
                self.assertNotIn("other workspace evidence", json.dumps(payload, ensure_ascii=False))
                self.assertEqual(runtime["capabilities"]["mode"], "remote_agent_planner")
                self.assertEqual(runtime["capabilities"]["chat_only_upstream_role"], "synthesis_only")
                self.assertIn("downstream_client", runtime["capabilities"]["ownership_model"])

                events_req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/agent-runtime-events.json?limit=10&tenant_contains=user-runtime&event_type=memory_rollup",
                    headers={"authorization": f"Basic {token}"},
                )
                with urllib.request.urlopen(events_req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    events_payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(len(events_payload["events"]), 1)
                self.assertEqual(events_payload["events"][0]["event_type"], "memory_rollup")
                self.assertEqual(events_payload["events"][0]["tenant_key"], "user-runtime")
                self.assertIn("runtime rollup", events_payload["events"][0]["summary"])
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if planner is not None:
                    planner._STORE = None
                gateway.SQLITE_READY = old_ready
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_admin_agent_capabilities_endpoint_exposes_ownership_model(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            httpd = None
            thread = None
            try:
                cfg = gateway._default_config()
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [{
                        "name": "get_weather",
                        "description": "Get current weather",
                        "method": "GET",
                        "url": "https://weather.example.test/current",
                        "input_schema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    }],
                }
                gateway.save_config(cfg)
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                token = base64.b64encode(b"admin:admin").decode("ascii")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/agent-capabilities.json",
                    headers={"authorization": f"Basic {token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))

                self.assertEqual(payload["mode"], "remote_agent_planner")
                self.assertEqual(payload["chat_only_upstream_role"], "synthesis_only")
                workflow_names = {item["name"] for item in payload["workflows"]}
                self.assertIn("project_analysis", workflow_names)
                self.assertIn("chat_only_synthesis", workflow_names)
                intent_kinds = {item["kind"] for item in payload["intents"]}
                self.assertIn("project_analysis", intent_kinds)
                self.assertIn("plain_chat", intent_kinds)
                project_intent = next(item for item in payload["intents"] if item["kind"] == "project_analysis")
                self.assertEqual(project_intent["workflow"], "project_analysis")
                self.assertEqual(project_intent["dispatch"], "downstream_client")
                project_workflow = next(item for item in payload["workflows"] if item["name"] == "project_analysis")
                self.assertEqual(project_workflow["owner"], "agent_planner")
                self.assertIn("core_flow_trace", project_workflow["steps"])
                self.assertEqual(project_workflow["plan_items"][0]["status"], "in_progress")
                transition_steps = [item["step"] for item in project_workflow["transitions"]]
                self.assertEqual(transition_steps[:3], ["planner_progress", "codebase_onboarding", "project_structure"])
                transition_builders = {item["builder"] for item in project_workflow["transitions"]}
                self.assertIn("core_flow_trace", transition_builders)
                self.assertIn("symbol_deep_dive", transition_builders)
                fix_workflow = next(item for item in payload["workflows"] if item["name"] == "fix_loop")
                fix_steps = [item["step"] for item in fix_workflow["transitions"]]
                self.assertIn("diagnostic_read", fix_steps)
                self.assertIn("source_followup_read", fix_steps)
                qa_workflow = next(item for item in payload["workflows"] if item["name"] == "qa_loop")
                qa_steps = [item["step"] for item in qa_workflow["transitions"]]
                self.assertIn("validate_after_test", qa_steps)
                self.assertIn("validate_after_build", qa_steps)
                code_search_workflow = next(item for item in payload["workflows"] if item["name"] == "code_search")
                code_search_transitions = code_search_workflow["transitions"]
                self.assertEqual(code_search_transitions[0]["condition"], "code_search_without_existing_search")
                self.assertEqual(code_search_transitions[0]["builder"], "code_search")
                test_build_workflow = next(item for item in payload["workflows"] if item["name"] == "test_build")
                test_build_steps = [item["step"] for item in test_build_workflow["transitions"]]
                self.assertEqual(test_build_steps, ["run_test", "run_build"])
                generic_workflow = next(item for item in payload["workflows"] if item["name"] == "generic_tool")
                generic_steps = [item["step"] for item in generic_workflow["transitions"]]
                self.assertEqual(generic_steps, ["skill_request", "shell_command", "read_file", "list_directory", "web_search", "custom_function"])
                edit_workflow = next(item for item in payload["workflows"] if item["name"] == "edit")
                edit_steps = [item["step"] for item in edit_workflow["transitions"]]
                self.assertEqual(edit_steps, ["edit_file", "write_file"])
                service_names = {item["name"] for item in payload["service_side"]}
                downstream_names = {item["name"] for item in payload["downstream_owned"]}
                self.assertIn("calculator", service_names)
                self.assertIn("get_weather", service_names)
                self.assertIn("image_generation", service_names)
                self.assertIn("Read", downstream_names)
                self.assertIn("Bash", downstream_names)
                self.assertIn("computer_use", downstream_names)
                self.assertNotIn("image_generation", downstream_names)
                weather = next(item for item in payload["http_actions"] if item["name"] == "get_weather")
                self.assertEqual(weather["owner"], "gateway_service")
                self.assertEqual(weather["schema"]["required"], ["city"])
                self.assertGreaterEqual(payload["counts"]["service_side"], 1)
                self.assertGreaterEqual(payload["counts"]["intents"], 1)
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config

    def test_admin_agent_runtime_audit_proves_scoped_remote_requirements(self):
        from src.gateway_context import _sqlite_insert_memory
        from src.gateway_agent_planner import record_runtime_event

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(pathlib.Path(td) / "service-workspace")
            pathlib.Path(os.environ["GATEWAY_WORKSPACE_ROOT"]).mkdir()
            gateway.SQLITE_READY = False
            httpd = None
            thread = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                cfg = gateway._default_config()
                cfg["gateway"]["agent_planner_strict_every_turn"] = True
                gateway.save_config(cfg)
                session_key = "/v1/messages:/client/audit-workspace:tenant:audit-user:session_id:audit-session"
                planner._store().save(
                    session_key,
                    {
                        "tenant_key": "audit-user",
                        "workspace_key": "/client/audit-workspace",
                        "workflow": "project_analysis",
                        "current_step": "synthesis",
                        "completed_steps": ["Skill", "search_graph", "Read"],
                        "evidence_count": 4,
                        "evidence_summary": "audit scoped project evidence",
                    },
                )
                for event_type, workflow, step, summary, metadata in (
                    ("intent_classification", "project_analysis", "classify_intent", "project analysis selected", {"kind": "project_analysis"}),
                    ("tool_dispatch", "project_analysis", "Read", "downstream client read requested", {"owner": "downstream_client", "tool": "Read"}),
                    ("gateway_tool_execute", "gateway_owned_tool", "preexecute_gateway_owned_tool", "calculator running", {"owner": "gateway_service", "tool": "calculator"}),
                    ("gateway_tool_result", "gateway_owned_tool", "preexecute_gateway_owned_tool", "calculator succeeded", {"owner": "gateway_service", "tool": "calculator", "success": True}),
                    ("memory_rollup", "memory", "rollup", "audit rollup created", {"kind": "session_rollup"}),
                    ("chat_only_synthesis_boundary", "chat_only_synthesis", "strip_upstream_tools", "non-streaming tools stripped", {"source": "non_streaming", "tool_authority_granted": False}),
                    ("chat_only_synthesis_boundary", "chat_only_synthesis", "strip_upstream_tools", "streaming tools stripped", {"source": "streaming", "tool_authority_granted": False}),
                    ("upstream_tool_attempt_ignored", "chat_only_synthesis", "strip_upstream_tools", "upstream pseudo tool ignored", {"tool_authority_granted": False}),
                ):
                    record_runtime_event(
                        session_key=session_key,
                        tenant_key="audit-user",
                        workspace_key="/client/audit-workspace",
                        event_type=event_type,
                        workflow=workflow,
                        step=step,
                        summary=summary,
                        metadata=metadata,
                    )
                record_runtime_event(
                    session_key="/v1/messages:/client/other-audit:tenant:other-user:session_id:other-session",
                    tenant_key="other-user",
                    workspace_key="/client/other-audit",
                    event_type="tool_dispatch",
                    workflow="project_analysis",
                    step="Read",
                    summary="OTHER_TENANT_AUDIT_MARKER",
                    metadata={"owner": "downstream_client"},
                )
                _sqlite_insert_memory(
                    "tenant:audit-user:session:audit-session",
                    "/client/audit-workspace",
                    "session_rollup",
                    "audit scoped rollup",
                    ["audit"],
                    None,
                    5,
                )
                _sqlite_insert_memory(
                    "tenant:other-user:session:other-session",
                    "/client/other-audit",
                    "session_rollup",
                    "OTHER_TENANT_MEMORY_MARKER",
                    ["other"],
                    None,
                    5,
                )

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                base_url = f"http://127.0.0.1:{httpd.server_address[1]}/admin/agent-runtime-audit.json"
                token = base64.b64encode(b"admin:admin").decode("ascii")

                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(urllib.request.Request(f"{base_url}?limit=100"), timeout=5)
                self.assertEqual(err.exception.code, 401)

                req = urllib.request.Request(
                    f"{base_url}?limit=100&tenant_contains=audit-user&workspace_contains=/client/audit-workspace&session_contains=audit-session",
                    headers={"authorization": f"Basic {token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))

                audit = payload["audit"]
                self.assertEqual(audit["mode"], "remote_agent_planner")
                self.assertEqual(audit["overall_status"], "proven/current_scope")
                self.assertEqual(audit["summary"]["missing"], 0)
                requirements = audit["requirements"]
                expected_keys = {
                    "agent_planner_runtime_mode",
                    "chat_only_upstream_config",
                    "downstream_client_tool_execution_policy",
                    "chat_only_upstream_synthesis_only",
                    "planner_owns_intent_and_workflows",
                    "strict_every_turn_planner_envelope",
                    "downstream_client_workspace_tools",
                    "gateway_owned_service_tools",
                    "infinite_context_memory_rollup",
                    "tenant_workspace_isolation",
                    "streaming_nonstreaming_parity",
                    "admin_observability",
                }
                self.assertEqual(set(requirements), expected_keys)
                for key in expected_keys:
                    self.assertEqual(requirements[key]["status"], "proven/current_scope", key)
                self.assertFalse(requirements["agent_planner_runtime_mode"]["detail"]["legacy_gateway_passthrough"])
                self.assertFalse(requirements["chat_only_upstream_config"]["detail"]["upstream_native_tool_authority"])
                self.assertFalse(requirements["downstream_client_tool_execution_policy"]["detail"]["gateway_forces_local_user_side_tools"])
                self.assertTrue(requirements["strict_every_turn_planner_envelope"]["detail"]["agent_planner_strict_every_turn"])
                self.assertEqual(requirements["strict_every_turn_planner_envelope"]["detail"]["missing_session_count"], 0)
                self.assertEqual(requirements["chat_only_upstream_synthesis_only"]["detail"]["tool_authority_granted"], False)
                self.assertEqual(requirements["tenant_workspace_isolation"]["detail"]["filters"]["workspace_contains"], "/client/audit-workspace")
                self.assertEqual(
                    requirements["streaming_nonstreaming_parity"]["detail"]["seen_synthesis_sources"],
                    ["non_streaming", "streaming"],
                )
                dumped = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("OTHER_TENANT_AUDIT_MARKER", dumped)
                self.assertNotIn("OTHER_TENANT_MEMORY_MARKER", dumped)
                self.assertNotIn(str(pathlib.Path(td) / "service-workspace"), dumped)
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                planner._STORE = None
                gateway.SQLITE_READY = old_ready
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_agent_runtime_audit_ignores_stale_sessions_outside_event_window(self):
        from src.gateway_http_handler import _agent_runtime_requirement_audit
        from src.gateway_tool_runtime import planner_capability_catalog

        stale_session = {
            "id": 1,
            "session_key": "/v1/messages:/client/old:tenant:old-user:session_id:old",
            "tenant_key": "old-user",
            "workspace_key": "/client/old",
            "workflow": "chat_only_synthesis",
            "current_step": "intent_classification",
        }
        current_session_key = "/v1/messages:/client/current:tenant:current-user:session_id:current"
        current_session = {
            "id": 2,
            "session_key": current_session_key,
            "tenant_key": "current-user",
            "workspace_key": "/client/current",
            "workflow": "project_analysis",
            "current_step": "project_structure",
        }

        def event(event_type, workflow, *, source=None, owner=None, session_key=current_session_key):
            metadata = {}
            if source:
                metadata["source"] = source
            if owner:
                metadata["owner"] = owner
            return {
                "id": len(events) + 1,
                "session_key": session_key,
                "tenant_key": "current-user",
                "workspace_key": "/client/current",
                "event_type": event_type,
                "workflow": workflow,
                "step": "step",
                "summary": event_type,
                "metadata": metadata,
            }

        events = []
        events.append(event("intent_classification", "project_analysis"))
        events.append(event("tool_dispatch", "project_analysis", owner="downstream_client"))
        events.append(event("gateway_tool_execute", "gateway_owned_tool", owner="gateway_service"))
        events.append(event("gateway_tool_result", "gateway_owned_tool", owner="gateway_service"))
        events.append(event("memory_rollup", "memory"))
        events.append(event("chat_only_synthesis_boundary", "chat_only_synthesis", source="non_streaming"))
        events.append(event("chat_only_synthesis_boundary", "chat_only_synthesis", source="streaming"))

        audit = _agent_runtime_requirement_audit(
            capabilities=planner_capability_catalog(include_mcp_tools=False),
            sessions=[stale_session, current_session],
            memories=[{"id": 1, "kind": "session_rollup", "summary": "rollup", "memory_session_key": current_session_key}],
            events=events,
            filters={
                "tenant_contains": "current-user",
                "workspace_contains": "/client/current",
                "session_contains": "current",
                "workflow": None,
                "current_step": None,
                "memory_kind": None,
                "event_type": None,
            },
            runtime_config={
                "gateway_tool_mode": "orchestrate",
                "agent_planner_strict_every_turn": True,
                "gateway_execute_user_side_tools": False,
                "gateway_delegate_tools_to_downstream": None,
                "upstream_tools_enabled": "adapter",
                "upstream_supports_tools": False,
                "upstream_supports_function_calls": False,
            },
        )

        requirements = audit["requirements"]
        strict = requirements["strict_every_turn_planner_envelope"]
        self.assertEqual(strict["status"], "proven/current_scope")
        self.assertEqual(strict["detail"]["session_count"], 1)
        self.assertEqual(strict["detail"]["stored_session_count"], 2)
        self.assertEqual(strict["detail"]["missing_session_count"], 0)
        self.assertEqual(requirements["tenant_workspace_isolation"]["status"], "proven/current_scope")
        self.assertEqual(audit["summary"]["missing"], 0)
        self.assertEqual(audit["overall_status"], "proven/current_scope")

    def test_agent_runtime_audit_global_view_does_not_fail_on_unscoped_historical_anonymous_sessions(self):
        from src.gateway_http_handler import _agent_runtime_requirement_audit
        from src.gateway_tool_runtime import planner_capability_catalog

        historical_key = "/v1/chat/completions:/service/anon:tenant:anonymous:anon:old"
        audit = _agent_runtime_requirement_audit(
            capabilities=planner_capability_catalog(include_mcp_tools=False),
            sessions=[{
                "id": 1,
                "session_key": historical_key,
                "tenant_key": "anonymous",
                "workspace_key": "/service/anon",
                "workflow": "chat_only_synthesis",
                "current_step": "intent_classification",
            }],
            memories=[],
            events=[{
                "id": 1,
                "session_key": historical_key,
                "tenant_key": "anonymous",
                "workspace_key": "/service/anon",
                "event_type": "intent_classification",
                "workflow": "chat_only_synthesis",
                "step": "intent_classification",
                "summary": "old anonymous plain chat classified before boundary instrumentation",
                "metadata": {"intent": {"kind": "plain_chat"}},
            }],
            filters={
                "tenant_contains": None,
                "workspace_contains": None,
                "session_contains": None,
                "workflow": None,
                "current_step": None,
                "memory_kind": None,
                "event_type": None,
            },
            runtime_config={
                "gateway_tool_mode": "orchestrate",
                "agent_planner_strict_every_turn": True,
                "gateway_execute_user_side_tools": False,
                "gateway_delegate_tools_to_downstream": None,
                "upstream_tools_enabled": "adapter",
                "upstream_supports_tools": False,
                "upstream_supports_function_calls": False,
            },
        )

        strict = audit["requirements"]["strict_every_turn_planner_envelope"]
        self.assertEqual(strict["status"], "configured/static")
        self.assertFalse(strict["detail"]["strict_runtime_scope"])
        self.assertEqual(strict["detail"]["unscoped_intent_session_count"], 1)
        self.assertEqual(strict["detail"]["missing_session_count"], 0)


    def test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions(self):
        from src.gateway_http_handler import _agent_runtime_requirement_audit
        from src.gateway_tool_runtime import planner_capability_catalog

        audit = _agent_runtime_requirement_audit(
            capabilities=planner_capability_catalog(include_mcp_tools=False),
            sessions=[],
            memories=[],
            events=[],
            filters={
                "tenant_contains": None,
                "workspace_contains": None,
                "session_contains": None,
                "workflow": None,
                "current_step": None,
                "memory_kind": None,
                "event_type": None,
            },
            runtime_config={
                "gateway_tool_mode": "orchestrate",
                "agent_planner_strict_every_turn": True,
                "gateway_execute_user_side_tools": False,
                "gateway_delegate_tools_to_downstream": None,
                "upstream_tools_enabled": "adapter",
                "upstream_supports_tools": False,
                "upstream_supports_function_calls": False,
            },
        )

        contract = audit["scope_contract"]
        self.assertEqual(contract["strict_conversation_scope"], "supported_authenticated_public_api_paths")
        self.assertIn("/v1/chat/completions", contract["conversation_paths"])
        self.assertIn("/v1/messages", contract["conversation_paths"])
        self.assertIn("/v1/responses", contract["conversation_paths"])
        self.assertIn("/anthropic/v1/chat/completions", contract["conversation_paths"])
        self.assertIn("/anthropic/v1/messages", contract["conversation_paths"])
        self.assertIn("/anthropic/v1/responses", contract["conversation_paths"])
        self.assertNotIn("/v1/assistants", contract["conversation_paths"])
        self.assertNotIn("/v1/threads", contract["conversation_paths"])
        self.assertIn("/v1/tools/call", contract["gateway_owned_service_paths"])
        self.assertIn("/v1/models", contract["gateway_owned_service_paths"])
        self.assertIn("/anthropic/v1/models", contract["gateway_owned_service_paths"])
        self.assertIn("/v1/assistants", contract["gateway_owned_service_paths"])
        self.assertIn("/v1/threads", contract["gateway_owned_service_paths"])
        self.assertIn("/admin/agent-runtime-audit.json", contract["control_plane_paths_excluded"])
        self.assertIn("/healthz", contract["control_plane_paths_excluded"])
        self.assertIn("auth_failures", contract["security_layer_excluded"])
        self.assertIn("unsupported_paths", contract["security_layer_excluded"])

    def test_admin_agent_runtime_audit_flags_non_strict_every_turn_mode(self):
        from src.gateway_http_handler import _agent_runtime_requirement_audit
        from src.gateway_tool_runtime import planner_capability_catalog

        session_key = "/v1/messages:/client/audit-workspace:tenant:audit-user:session_id:audit-session"
        audit = _agent_runtime_requirement_audit(
            capabilities=planner_capability_catalog(include_mcp_tools=False),
            sessions=[{
                "session_key": session_key,
                "tenant_key": "audit-user",
                "workspace_key": "/client/audit-workspace",
                "workflow": "chat_only_synthesis",
                "current_step": "synthesis",
                "evidence_count": 0,
            }],
            memories=[],
            events=[
                {
                    "session_key": session_key,
                    "tenant_key": "audit-user",
                    "workspace_key": "/client/audit-workspace",
                    "event_type": "intent_classification",
                    "workflow": "chat_only_synthesis",
                    "step": "intent_classification",
                    "summary": "plain chat classified",
                    "metadata": {"intent": {"kind": "plain_chat"}},
                }
            ],
            filters={"tenant_contains": "audit-user", "workspace_contains": "/client/audit-workspace", "session_contains": "audit-session"},
            runtime_config={
                "gateway_tool_mode": "orchestrate",
                "agent_planner_strict_every_turn": False,
                "upstream_tools_enabled": "adapter",
                "upstream_supports_tools": False,
                "upstream_supports_function_calls": False,
                "gateway_execute_user_side_tools": False,
                "gateway_delegate_tools_to_downstream": None,
            },
        )
        req = audit["requirements"]["strict_every_turn_planner_envelope"]
        self.assertEqual(req["status"], "missing/current_scope")
        self.assertFalse(req["detail"]["agent_planner_strict_every_turn"])
        self.assertEqual(audit["overall_status"], "needs_runtime_evidence")

    def test_admin_agent_runtime_audit_flags_legacy_passthrough_mode(self):
        from src.gateway_agent_planner import record_runtime_event

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            httpd = None
            thread = None
            planner = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "passthrough"
                gateway.save_config(cfg)
                record_runtime_event(
                    session_key="/v1/messages:/client/legacy:tenant:legacy-user:session_id:legacy-session",
                    tenant_key="legacy-user",
                    workspace_key="/client/legacy",
                    event_type="planner_state",
                    workflow="project_analysis",
                    step="legacy_probe",
                    summary="legacy mode should not pass agent runtime audit",
                    metadata={"evidence_count": 1},
                )

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                token = base64.b64encode(b"admin:admin").decode("ascii")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/agent-runtime-audit.json?limit=20&tenant_contains=legacy-user&workspace_contains=/client/legacy&session_contains=legacy-session",
                    headers={"authorization": f"Basic {token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))

                audit = payload["audit"]
                mode_req = audit["requirements"]["agent_planner_runtime_mode"]
                self.assertEqual(mode_req["status"], "missing/current_scope")
                self.assertTrue(mode_req["detail"]["legacy_gateway_passthrough"])
                self.assertEqual(mode_req["detail"]["gateway_tool_mode"], "passthrough")
                self.assertEqual(audit["runtime_config"]["gateway_tool_mode"], "passthrough")
                self.assertEqual(audit["overall_status"], "needs_runtime_evidence")
                self.assertGreaterEqual(audit["summary"]["missing"], 1)
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if planner is not None:
                    planner._STORE = None
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime

    def test_admin_agent_runtime_audit_flags_upstream_native_tool_authority(self):
        from src.gateway_agent_planner import record_runtime_event

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            httpd = None
            thread = None
            planner = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "auto"
                cfg["upstream"]["capabilities"]["supports_tools"] = True
                cfg["upstream"]["capabilities"]["supports_function_calls"] = True
                gateway.save_config(cfg)
                record_runtime_event(
                    session_key="/v1/messages:/client/native-upstream:tenant:native-user:session_id:native-session",
                    tenant_key="native-user",
                    workspace_key="/client/native-upstream",
                    event_type="planner_state",
                    workflow="project_analysis",
                    step="native_probe",
                    summary="native upstream should not pass chat-only audit",
                    metadata={"evidence_count": 1},
                )

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                token = base64.b64encode(b"admin:admin").decode("ascii")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/agent-runtime-audit.json?limit=20&tenant_contains=native-user&workspace_contains=/client/native-upstream&session_contains=native-session",
                    headers={"authorization": f"Basic {token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))

                audit = payload["audit"]
                native_req = audit["requirements"]["chat_only_upstream_config"]
                self.assertEqual(native_req["status"], "missing/current_scope")
                self.assertTrue(native_req["detail"]["upstream_native_tool_authority"])
                self.assertTrue(native_req["detail"]["upstream_supports_tools"])
                self.assertTrue(native_req["detail"]["upstream_supports_function_calls"])
                self.assertEqual(native_req["detail"]["upstream_tools_enabled"], "auto")
                self.assertTrue(audit["runtime_config"]["upstream_native_tool_authority"])
                self.assertEqual(audit["overall_status"], "needs_runtime_evidence")
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if planner is not None:
                    planner._STORE = None
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime

    def test_admin_agent_runtime_audit_flags_gateway_user_side_tool_execution(self):
        from src.gateway_agent_planner import record_runtime_event

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            httpd = None
            thread = None
            planner = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["gateway"]["execute_user_side_tools_in_gateway"] = True
                gateway.save_config(cfg)
                record_runtime_event(
                    session_key="/v1/messages:/client/local-tools:tenant:local-tools-user:session_id:local-tools-session",
                    tenant_key="local-tools-user",
                    workspace_key="/client/local-tools",
                    event_type="tool_dispatch",
                    workflow="generic_tool",
                    step="read_file",
                    summary="downstream tool event should not override unsafe local execution policy",
                    metadata={"owner": "downstream_client", "tool": "Read"},
                )

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                token = base64.b64encode(b"admin:admin").decode("ascii")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/admin/agent-runtime-audit.json?limit=20&tenant_contains=local-tools-user&workspace_contains=/client/local-tools&session_contains=local-tools-session",
                    headers={"authorization": f"Basic {token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))

                audit = payload["audit"]
                policy_req = audit["requirements"]["downstream_client_tool_execution_policy"]
                self.assertEqual(policy_req["status"], "missing/current_scope")
                self.assertTrue(policy_req["detail"]["gateway_execute_user_side_tools"])
                self.assertTrue(policy_req["detail"]["gateway_forces_local_user_side_tools"])
                self.assertTrue(audit["runtime_config"]["gateway_forces_local_user_side_tools"])
                self.assertEqual(audit["overall_status"], "needs_runtime_evidence")
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if planner is not None:
                    planner._STORE = None
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime

    def test_admin_agent_runtime_audit_delegate_false_does_not_flag_cloud_local_user_side_execution(self):
        from src.gateway_http_handler import _agent_runtime_requirement_audit

        audit = _agent_runtime_requirement_audit(
            capabilities={
                "ownership_model": {"downstream_client": [], "gateway_service": []},
                "downstream_owned": ["Read"],
                "service_side": ["calculator"],
                "workflows": ["generic_tool"],
                "intents": ["tool_request"],
                "chat_only_upstream_role": "synthesis_only",
            },
            sessions=[],
            memories=[],
            events=[],
            filters={},
            runtime_config={
                "gateway_tool_mode": "orchestrate",
                "agent_planner_strict_every_turn": False,
                "gateway_execute_user_side_tools": False,
                "gateway_delegate_tools_to_downstream": False,
                "upstream_tools_enabled": "adapter",
                "upstream_supports_tools": False,
                "upstream_supports_function_calls": False,
            },
        )
        policy_req = audit["requirements"]["downstream_client_tool_execution_policy"]
        self.assertEqual(policy_req["status"], "configured/static")
        self.assertFalse(policy_req["detail"]["gateway_execute_user_side_tools"])
        self.assertFalse(policy_req["detail"]["gateway_forces_local_user_side_tools"])
        self.assertFalse(audit["runtime_config"]["gateway_forces_local_user_side_tools"])

    def test_admin_ui_renders_agent_runtime_events_table(self):
        from src.gateway_agent_planner import record_runtime_event
        from src.gateway_admin import _render_admin_ui

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            planner = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                gateway.save_config(gateway._default_config())
                record_runtime_event(
                    session_key="/v1/messages:/client/admin-ui:tenant:ui-user:session_id:ui-session",
                    tenant_key="ui-user",
                    workspace_key="/client/admin-ui",
                    event_type="gateway_tool_result",
                    workflow="gateway_owned_tool",
                    step="preexecute_gateway_owned_tool",
                    summary="calculator succeeded for admin ui",
                    metadata={"tool": "calculator", "success": True},
                )

                html = _render_admin_ui()

                self.assertIn("Agent Runtime Events", html)
                self.assertIn("gateway_tool_result", html)
                self.assertIn("gateway_owned_tool", html)
                self.assertIn("calculator succeeded for admin ui", html)
                self.assertIn("ui-user", html)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if planner is not None:
                    planner._STORE = None

    def test_admin_memories_endpoint_filters_remote_scope(self):
        from src.gateway_context import _sqlite_insert_memory

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            gateway.SQLITE_READY = False
            httpd = None
            thread = None
            try:
                gateway.save_config(gateway._default_config())
                _sqlite_insert_memory(
                    "tenant:user-a:session:session-a",
                    "/client/workspace-a",
                    "conversation_turn",
                    "normal memory a",
                    ["normal"],
                    None,
                    1,
                )
                _sqlite_insert_memory(
                    "tenant:user-b:session:session-b",
                    "/client/workspace-b",
                    "session_rollup",
                    "rollup memory b",
                    ["rollup"],
                    None,
                    5,
                )

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                base_url = f"http://127.0.0.1:{httpd.server_address[1]}/admin/memories.json"
                token = base64.b64encode(b"admin:admin").decode("ascii")

                unauth = urllib.request.Request(f"{base_url}?limit=10")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(unauth, timeout=5)
                self.assertEqual(err.exception.code, 401)

                def fetch(query: str):
                    req = urllib.request.Request(
                        f"{base_url}?{query}",
                        headers={"authorization": f"Basic {token}"},
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        self.assertEqual(resp.status, 200)
                        return json.loads(resp.read().decode("utf-8"))

                rollups = fetch("limit=10&tenant_contains=user-b&has_rollup=1")
                self.assertEqual(len(rollups["memories"]), 1)
                self.assertEqual(rollups["memories"][0]["kind"], "session_rollup")
                self.assertEqual(rollups["memories"][0]["tenant_key"], "user-b")
                self.assertEqual(rollups["filters"]["tenant_contains"], "user-b")
                self.assertTrue(rollups["filters"]["has_rollup"])

                normal = fetch("limit=10&workspace_contains=workspace-a&session_contains=session-a&has_rollup=0")
                self.assertEqual(len(normal["memories"]), 1)
                self.assertEqual(normal["memories"][0]["summary"], "normal memory a")
                self.assertEqual(normal["memories"][0]["workspace_key"], "/client/workspace-a")

                kind_filtered = fetch("limit=10&kind=session_rollup")
                self.assertEqual(len(kind_filtered["memories"]), 1)
                self.assertEqual(kind_filtered["memories"][0]["summary"], "rollup memory b")
            finally:
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                if thread:
                    thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                gateway.SQLITE_READY = old_ready
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite

    def test_agent_planner_store_migrates_and_indexes_runtime_sessions(self):
        import sqlite3
        from src.gateway_agent_planner import AgentPlannerStore

        with tempfile.TemporaryDirectory() as td:
            db_path = pathlib.Path(td) / "agent_planner.sqlite3"
            legacy_state = {
                "session_key": "/v1/messages:/client/workspace:tenant:legacy-tenant:session_id:legacy-session",
                "workflow": "project_analysis",
                "current_step": "codebase_onboarding",
                "evidence_count": 0,
            }
            with sqlite3.connect(db_path) as con:
                con.execute(
                    "CREATE TABLE planner_sessions (session_key TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at REAL NOT NULL)"
                )
                con.execute(
                    "INSERT INTO planner_sessions(session_key, state_json, updated_at) VALUES(?,?,?)",
                    (legacy_state["session_key"], json.dumps(legacy_state), 1.0),
                )

            store = AgentPlannerStore(db_path)
            columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(planner_sessions)").fetchall()}
            self.assertTrue({"tenant_key", "workspace_key", "workflow", "current_step", "evidence_count"}.issubset(columns))

            migrated = store.list_recent(tenant_contains="legacy-tenant", workflow="project_analysis")
            self.assertEqual(len(migrated), 1)
            self.assertEqual(migrated[0]["tenant_key"], "legacy-tenant")
            self.assertEqual(migrated[0]["workspace_key"], "/client/workspace")

            store.save(
                "/v1/messages:/ignored:tenant:not-used:session_id:indexed-session",
                {
                    "tenant_key": "explicit-tenant",
                    "workspace_key": "/client/explicit",
                    "workflow": "fix_test",
                    "current_step": "pytest",
                    "evidence_count": 2,
                    "evidence_summary": "bounded",
                },
            )
            indexed = store.list_recent(tenant_contains="explicit-tenant", workflow="fix_test", has_evidence=True)
            self.assertEqual(len(indexed), 1)
            self.assertEqual(indexed[0]["tenant_key"], "explicit-tenant")
            self.assertEqual(indexed[0]["workspace_key"], "/client/explicit")
            self.assertEqual(indexed[0]["current_step"], "pytest")

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
                                    "allow_private_network": True,
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
                merged = _merge_builtin_tools(
                    "/v1/chat/completions",
                    {
                        "gateway_context": {"client_can_handle_implicit_tools": True},
                        "messages": [{"role": "user", "content": "inspect the project files"}],
                    },
                )
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
                            "allow_private_network": True,
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
                            "allow_private_network": True,
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
                            "allow_private_network": True,
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

    def test_http_action_private_network_url_requires_admin_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [
                        {
                            "name": "metadata_http",
                            "method": "GET",
                            "url": "http://127.0.0.1:9/metadata",
                        }
                    ],
                }
                gateway.save_config(cfg)
                result = _execute_tool_call(ToolCall("http_private", "metadata_http", {}, {}))
                self.assertFalse(result.success)
                self.assertEqual(result.failure_type, "invalid_input")
                self.assertIn("allow_private_network", result.content)
            finally:
                gateway.CONFIG_PATH = old_config

    def test_http_action_dns_private_target_requires_admin_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                cfg = gateway._default_config()
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [
                        {
                            "name": "metadata_dns_http",
                            "method": "GET",
                            "url": "http://metadata.example.test/metadata",
                        }
                    ],
                }
                gateway.save_config(cfg)
                with patch(
                    "src.gateway_http_actions.socket.getaddrinfo",
                    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 80))],
                ):
                    result = _execute_tool_call(ToolCall("http_dns_private", "metadata_dns_http", {}, {}))
                self.assertFalse(result.success)
                self.assertEqual(result.failure_type, "invalid_input")
                self.assertIn("allow_private_network", result.content)
            finally:
                gateway.CONFIG_PATH = old_config

    def test_http_action_redirect_to_private_network_requires_admin_opt_in(self):
        from src.gateway_http_actions import _HttpActionRedirectHandler

        handler = _HttpActionRedirectHandler({})
        req = urllib.request.Request("https://safe.example.test/start")
        with self.assertRaises(Exception) as cm:
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:9/metadata")
        self.assertEqual(getattr(cm.exception, "failure_type", None), "invalid_input")
        self.assertIn("allow_private_network", str(cm.exception))

    def test_webfetch_private_network_url_requires_admin_opt_in(self):
        result = _execute_tool_call(ToolCall("webfetch-private", "WebFetch", {"url": "http://127.0.0.1:9/metadata"}, {}))
        self.assertFalse(result.success)
        self.assertEqual(result.failure_type, "invalid_input")
        self.assertIn("allow_private_network", result.content)

    def test_webfetch_dns_private_target_requires_admin_opt_in(self):
        with patch(
            "src.gateway_http_actions.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 80))],
        ):
            result = _execute_tool_call(
                ToolCall("webfetch-dns-private", "WebFetch", {"url": "http://metadata.example.test/metadata"}, {})
            )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_type, "invalid_input")
        self.assertIn("allow_private_network", result.content)

    def test_websearch_private_search_url_requires_admin_opt_in(self):
        result = _execute_tool_call(
            ToolCall(
                "websearch-private",
                "WebSearch",
                {"query": "metadata", "search_url": "http://127.0.0.1:9/search"},
                {},
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_type, "invalid_input")
        self.assertIn("allow_private_network", result.content)

    def test_streaming_chat_request_requests_upstream_stream_in_orchestrate_mode(self):
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
                self.assertTrue(UpstreamHandler.seen_bodies[0]["stream"])
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
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
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


    def test_conversation_memory_store_migrates_and_indexes_remote_scope(self):
        import sqlite3
        from src.gateway_context import _sqlite_insert_memory, _sqlite_recall_memories, _sqlite_tail_memories

        with tempfile.TemporaryDirectory() as td:
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            db_path = pathlib.Path(td) / "memory.sqlite3"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(db_path)
            gateway.SQLITE_READY = False
            legacy_session = "tenant:user-a:session:session-a"
            legacy_workspace = "/client/workspace-a"
            try:
                with sqlite3.connect(db_path) as con:
                    con.execute(
                        """
                        CREATE TABLE conversation_memories (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts TEXT NOT NULL,
                            session_key TEXT NOT NULL,
                            workspace_root TEXT NOT NULL,
                            kind TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            keywords_json TEXT NOT NULL DEFAULT '[]',
                            source_request_id TEXT,
                            importance INTEGER NOT NULL DEFAULT 1,
                            last_used_at TEXT
                        )
                        """
                    )
                    con.execute(
                        """
                        INSERT INTO conversation_memories
                            (ts, session_key, workspace_root, kind, summary, keywords_json, source_request_id, importance, last_used_at)
                        VALUES ('2026-01-01T00:00:00+00:00', ?, ?, 'conversation_turn', 'legacy gateway memory', '["gateway"]', NULL, 3, NULL)
                        """,
                        (legacy_session, legacy_workspace),
                    )

                recalled = _sqlite_recall_memories(legacy_session, legacy_workspace, ["gateway"], 5)
                self.assertEqual(len(recalled), 1)
                self.assertIn("legacy gateway memory", recalled[0]["summary"])

                _sqlite_insert_memory(
                    "tenant:user-b:session:session-b",
                    "/client/workspace-b",
                    "session_rollup",
                    "new indexed rollup",
                    ["rollup"],
                    None,
                    5,
                )
                tail = _sqlite_tail_memories(10)
                indexed_new = next(item for item in tail if item["summary"] == "new indexed rollup")
                self.assertEqual(indexed_new["tenant_key"], "user-b")
                self.assertEqual(indexed_new["workspace_key"], "/client/workspace-b")
                self.assertEqual(indexed_new["memory_session_key"], "session:session-b")
                indexed_legacy = next(item for item in tail if item["summary"] == "legacy gateway memory")
                self.assertEqual(indexed_legacy["tenant_key"], "user-a")
                self.assertEqual(indexed_legacy["workspace_key"], legacy_workspace)
            finally:
                gateway.SQLITE_READY = old_ready
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite

    def test_conversation_memory_recalls_same_session_workspace_only(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            gateway.SQLITE_READY = False
            try:
                workspace = pathlib.Path(td) / "client-workspace"
                workspace.mkdir()
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
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

    def test_responses_conversation_memory_is_injected_into_input(self):
        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            gateway.SQLITE_READY = False
            try:
                workspace = pathlib.Path(td) / "client-workspace"
                workspace.mkdir()
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["context"]["memory_enabled"] = True
                cfg["context"]["memory_summary_max_chars"] = 500
                cfg["context"]["memory_recall_limit"] = 5
                cfg["upstream"]["tools_enabled"] = "off"
                cfg["gateway"]["local_planner_enabled"] = False
                gateway.save_config(cfg)

                metadata = {"session_id": "responses-memory-session", "user_id": json.dumps({"user_id": "responses-user"})}
                first_client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "Recorded Responses API decision."}}]}])
                run_tool_orchestration(
                    "/v1/responses",
                    {"model": "m", "metadata": metadata, "input": "Remember Responses API uses client workspace memory marker ALPHA-RSP"},
                    first_client,
                )

                recall_client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "Using Responses recalled memory."}}]}])
                run_tool_orchestration(
                    "/v1/responses",
                    {"model": "m", "metadata": metadata, "input": "What Responses API marker did we record?"},
                    recall_client,
                )
                sent = recall_client.requests[0][1]
                serialized = json.dumps(sent, ensure_ascii=False)
                self.assertIn("Gateway recalled memory", serialized)
                self.assertIn("ALPHA-RSP", serialized)
                self.assertIn("messages", sent)
            finally:
                gateway.CONFIG_PATH = old_config
                gateway.SQLITE_READY = old_ready
                if old_sqlite is None:
                    os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
                else:
                    os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite

    def test_streaming_responses_conversation_memory_is_injected_before_upstream(self):
        from src.gateway_context import _context_config
        from src.gateway_streaming import _run_streaming_orchestration_scoped

        events = []

        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)

                @staticmethod
                def flush():
                    pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_ready = gateway.SQLITE_READY
            old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(pathlib.Path(td) / "memory.sqlite3")
            gateway.SQLITE_READY = False
            try:
                workspace = pathlib.Path(td) / "client-workspace"
                workspace.mkdir()
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["context"]["memory_enabled"] = True
                cfg["context"]["memory_summary_max_chars"] = 500
                cfg["context"]["memory_recall_limit"] = 5
                cfg["gateway"]["local_planner_enabled"] = False
                cfg["upstream"]["tools_enabled"] = "off"
                cfg["upstream"]["protocol"] = "openai_chat"
                gateway.save_config(cfg)

                metadata = {"session_id": "stream-responses-memory", "user_id": json.dumps({"user_id": "stream-rsp-user"})}
                first_client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "Recorded STREAM-RSP-MARKER."}}]}])
                run_tool_orchestration(
                    "/v1/responses",
                    {"model": "m", "workspace_root": str(workspace), "metadata": metadata, "input": "Remember streaming responses marker STREAM-RSP-MARKER"},
                    first_client,
                )

                upstream = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "Streaming recall used memory."}, "finish_reason": "stop"}]}])
                _run_streaming_orchestration_scoped(
                    FakeHandler(),
                    "/v1/responses",
                    {"model": "m", "workspace_root": str(workspace), "metadata": metadata, "input": "What streaming responses marker did we record?", "stream": True},
                    mode="orchestrate",
                    upstream_protocol="openai_chat",
                    gateway_cfg=cfg["gateway"],
                    max_rounds=4,
                    upstream=upstream,
                    context_cfg=_context_config(),
                )

                self.assertTrue(upstream.requests)
                sent = upstream.requests[0][1]
                serialized = json.dumps(sent, ensure_ascii=False)
                self.assertIn("Gateway recalled memory", serialized)
                self.assertIn("STREAM-RSP-MARKER", serialized)
                self.assertIn("Streaming recall used memory", "".join(events))
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

    def test_conversation_memory_periodic_rollup_is_recalled(self):
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
                cfg["context"]["memory_rollup_every_turns"] = 2
                cfg["context"]["memory_rollup_max_chars"] = 1200
                cfg["context"]["memory_inject_max_chars"] = 4000
                cfg["gateway"]["local_planner_enabled"] = False
                gateway.save_config(cfg)

                base_body = {"model": "m", "metadata": {"session_id": "rollup-session"}}
                first_client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "Alpha decision recorded."}}]},
                    {"choices": [{"message": {"role": "assistant", "content": "Beta decision recorded."}}]},
                ])
                for text in ("Remember alpha architecture decision", "Remember beta test decision"):
                    body = dict(base_body)
                    body["messages"] = [{"role": "user", "content": text}]
                    run_tool_orchestration("/v1/chat/completions", body, first_client)

                memories = gateway._sqlite_tail_memories(10)
                rollups = [mem for mem in memories if mem["kind"] == "session_rollup"]
                self.assertEqual(len(rollups), 1)
                self.assertIn("Periodic conversation summary", rollups[0]["summary"])
                self.assertIn("alpha", rollups[0]["summary"].lower())
                self.assertIn("beta", rollups[0]["summary"].lower())

                recall_client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "Using recalled rollup."}}]},
                ])
                recall_body = dict(base_body)
                recall_body["messages"] = [{"role": "user", "content": "What did we decide earlier?"}]
                run_tool_orchestration("/v1/chat/completions", recall_body, recall_client)
                sent = recall_client.requests[0][1]
                serialized = json.dumps(sent["messages"], ensure_ascii=False)
                self.assertIn("Periodic conversation summary", serialized)
                self.assertIn("alpha", serialized.lower())
                self.assertIn("beta", serialized.lower())
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

                memory = execute_direct_tool_call({"workspace_root": td, "tool": "SaveMemory", "arguments": {"action": "write", "summary": "top tool aliases verified", "keywords": ["top-tools"]}})
                self.assertTrue(memory["success"])
                recalled = execute_direct_tool_call({"workspace_root": td, "tool": "RecallMemory", "arguments": {"action": "list", "limit": 5}})
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


    def test_streaming_entry_sets_remote_runtime_scope_from_request_body(self):
        from src.gateway_streaming import run_streaming_orchestration
        from src.gateway_builtin_tools import _runtime_scope_key, _workspace_root

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
            captured = {}
            try:
                workspace = pathlib.Path(td) / "client-workspace"
                workspace.mkdir()
                os.environ.pop("GATEWAY_TOOL_MODE", None)
                cfg = gateway._default_config()
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
                cfg["gateway"]["tool_mode"] = "orchestrate"
                gateway.save_config(cfg)

                def fake_scoped(*args, **kwargs):
                    captured["scope"] = _runtime_scope_key()
                    captured["workspace"] = str(_workspace_root())

                with patch("src.gateway_streaming._run_streaming_orchestration_scoped", side_effect=fake_scoped):
                    run_streaming_orchestration(
                        FakeHandler(),
                        "/v1/chat/completions",
                        {
                            "model": "m",
                            "stream": True,
                            "workspace_root": str(workspace),
                            "metadata": {
                                "session_id": "stream-session",
                                "user_id": json.dumps({"user_id": "stream-user"}),
                            },
                            "messages": [{"role": "user", "content": "hi"}],
                        },
                    )

                self.assertEqual(captured["workspace"], str(workspace.resolve()))
                self.assertIn("tenant:stream-user", captured["scope"])
                self.assertIn("session:stream-session", captured["scope"])
                self.assertIn("workspace:", captured["scope"])
                self.assertNotIn("workspace:no-workspace", captured["scope"])
            finally:
                gateway.CONFIG_PATH = old_config
                if old_mode is None:
                    os.environ.pop("GATEWAY_TOOL_MODE", None)
                else:
                    os.environ["GATEWAY_TOOL_MODE"] = old_mode


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

    def test_streaming_text_tool_fallback_surfaces_declared_user_side_tool(self):
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
                cfg["gateway"]["agent_planner_strict_every_turn"] = False
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["protocol"] = "openai_chat"
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
                with patch("src.gateway_proxy.NativeProxyClient", return_value=client):
                    run_streaming_orchestration(
                        FakeHandler(),
                        "/v1/chat/completions",
                        {
                            "model": "m",
                            "stream": True,
                            "messages": [{"role": "user", "content": "Please continue."}],
                            "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}],
                        },
                    )

                text = "".join(events)
                self.assertEqual(len(client.requests), 1)
                self.assertIn('"tool_calls"', text)
                self.assertIn('"name": "Bash"', text)
                self.assertIn('\\"command\\": \\"pwd\\"', text)
                self.assertIn('"finish_reason": "tool_calls"', text)
                self.assertNotIn("event: error", text)
                self.assertNotIn("tool_start", text)
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
                self.assertIn('"gateway_context"', text)
                self.assertIn('"intent": {"kind": "shell_command"', text)
                self.assertIn('"workflow": "generic_tool"', text)
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

    def test_streaming_gateway_owned_http_action_preexecutes_before_upstream(self):
        from src.gateway_streaming import run_streaming_orchestration

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
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), WeatherHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                os.environ.pop("GATEWAY_TOOL_MODE", None)
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                cfg["http_actions"] = {
                    "enabled": True,
                    "actions": [{
                        "name": "get_weather",
                        "description": "Get current weather",
                        "method": "GET",
                        "url": f"http://127.0.0.1:{httpd.server_address[1]}/weather",
                        "allow_private_network": True,
                        "input_schema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    }],
                }
                gateway.save_config(cfg)
                client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "Shanghai weather is sunny, 21C."}, "finish_reason": "stop"}]},
                ])
                with patch("src.gateway_proxy.NativeProxyClient", return_value=client):
                    run_streaming_orchestration(
                        FakeHandler(),
                        "/v1/chat/completions",
                        {
                            "model": "m",
                            "stream": True,
                            "messages": [{"role": "user", "content": "Weather in Shanghai?"}],
                        },
                    )

                self.assertEqual(WeatherHandler.seen[0]["city"], ["Shanghai"])
                self.assertEqual(len(client.requests), 1)
                self.assertIn("temp_c", client.requests[0][1]["messages"][-1]["content"])
                self.assertNotIn("tools", client.requests[0][1])
                text = "".join(events)
                self.assertIn("Shanghai weather is sunny, 21C.", text)
                self.assertIn("[DONE]", text)
                self.assertNotIn("event: error", text)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                gateway.CONFIG_PATH = old_config
                if old_mode is None:
                    os.environ.pop("GATEWAY_TOOL_MODE", None)
                else:
                    os.environ["GATEWAY_TOOL_MODE"] = old_mode

    def test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream(self):
        from src.gateway_streaming import run_streaming_orchestration
        from src.gateway_agent_planner import list_runtime_events

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
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            planner = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                os.environ.pop("GATEWAY_TOOL_MODE", None)
                cfg = gateway._default_config()
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)
                client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "The result is 42."}, "finish_reason": "stop"}]},
                ])
                with patch("src.gateway_proxy.NativeProxyClient", return_value=client):
                    run_streaming_orchestration(
                        FakeHandler(),
                        "/v1/chat/completions",
                        {
                            "model": "m",
                            "stream": True,
                            "metadata": {
                                "session_id": "stream-client-calc-session",
                                "user_id": json.dumps({"user_id": "stream-client-calc-user"}),
                            },
                            "messages": [{"role": "user", "content": "Calculate 6*7 for me"}],
                        },
                        client_id="stream-client-key",
                    )

                self.assertEqual(len(client.requests), 1)
                sent = client.requests[0][1]
                self.assertIn("42", sent["messages"][-1]["content"])
                self.assertNotIn("gateway_context", sent)
                self.assertNotIn("gateway_agent_planner", sent)
                self.assertNotIn("tools", sent)
                text = "".join(events)
                self.assertIn("The result is 42.", text)
                self.assertIn('"gateway_context"', text)
                self.assertIn('"tool": "calculator"', text)
                self.assertIn("[DONE]", text)
                self.assertNotIn("event: error", text)
                runtime_events = list_runtime_events(
                    10,
                    tenant_contains="stream-client-calc-user",
                    event_type="gateway_tool_execute",
                )
                self.assertEqual(len(runtime_events), 1)
                self.assertTrue(runtime_events[0]["metadata"]["client_id_present"])
            finally:
                gateway.CONFIG_PATH = old_config
                if old_mode is None:
                    os.environ.pop("GATEWAY_TOOL_MODE", None)
                else:
                    os.environ["GATEWAY_TOOL_MODE"] = old_mode
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if planner is not None:
                    planner._STORE = None

    def test_streaming_configured_mcp_tool_preexecutes_without_request_tools(self):
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
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)
                client = FakeClient([
                    {"choices": [{"message": {"role": "assistant", "content": "MCP streaming final ok."}, "finish_reason": "stop"}]},
                ])
                with patch("src.gateway_proxy.NativeProxyClient", return_value=client), \
                    patch("src.gateway_tool_runtime._enabled_mcp_servers", return_value=[{"name": "test", "enabled": True}]), \
                    patch("src.gateway_tool_runtime._mcp_list_server_tools", return_value=[{
                        "name": "echo_mcp",
                        "description": "Echo via MCP",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    }]), \
                    patch("src.gateway_tool_runtime._mcp_server_by_name", return_value={"name": "test"}), \
                    patch("src.gateway_tool_runtime._mcp_call_tool", side_effect=lambda _server, _tool, args: "mcp:" + str(args.get("value", ""))):
                    run_streaming_orchestration(
                        FakeHandler(),
                        "/v1/chat/completions",
                        {
                            "model": "m",
                            "stream": True,
                            "messages": [{"role": "user", "content": "Echo via MCP value ok"}],
                        },
                    )

                self.assertEqual(len(client.requests), 1)
                sent = client.requests[0][1]
                self.assertIn("mcp:Echo via MCP value ok", sent["messages"][-1]["content"])
                self.assertNotIn("tools", sent)
                text = "".join(events)
                self.assertIn("MCP streaming final ok.", text)
                self.assertIn("[DONE]", text)
                self.assertNotIn("event: error", text)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_mode is None:
                    os.environ.pop("GATEWAY_TOOL_MODE", None)
                else:
                    os.environ["GATEWAY_TOOL_MODE"] = old_mode

    def test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools(self):
        from src.gateway_streaming import run_streaming_orchestration

        def run_once(body):
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

            run_streaming_orchestration(FakeHandler(), "/v1/messages", body)
            return "".join(events)

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_mode = os.environ.get("GATEWAY_TOOL_MODE")
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_project = os.environ.get("GATEWAY_CODEBASE_MEMORY_PROJECT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                workspace = pathlib.Path(td) / "workspace"
                workspace.mkdir()
                os.environ["GATEWAY_WORKSPACE_ROOT"] = str(workspace)
                os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = "Users-sanbo-Desktop-ai_tool_functioncall"
                os.environ.pop("GATEWAY_TOOL_MODE", None)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                base_tools = [{
                    "name": "Skill",
                    "input_schema": {
                        "type": "object",
                        "properties": {"skill": {"type": "string"}, "args": {"type": "string"}},
                        "required": ["skill"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "mcp__codebase_memory_mcp__search_graph",
                    "input_schema": {
                        "type": "object",
                        "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                        "required": ["project", "query"],
                        "additionalProperties": False,
                    },
                }]
                user_text = f"分析这套项目 streaming planner {uuid.uuid4().hex}"
                first = run_once({
                    "model": "m",
                    "stream": True,
                    "messages": [
                        {"role": "system", "content": "Available skills:\n- codebase-onboarding"},
                        {"role": "user", "content": user_text},
                    ],
                    "tools": base_tools,
                    "max_tokens": 256,
                })
                self.assertIn('"type": "tool_use"', first)
                self.assertIn('"name": "Skill"', first)
                self.assertIn("codebase-onboarding", first)
                self.assertIn('"stop_reason": "tool_use"', first)
                self.assertNotIn("event: error", first)

                second = run_once({
                    "model": "m",
                    "stream": True,
                    "messages": [
                        {"role": "system", "content": "Available skills:\n- codebase-onboarding"},
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "planner_codebase_onboarding_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "Skill", "input": {"skill": "codebase-onboarding"}},
                        ]},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "planner_codebase_onboarding_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "content": "Successfully loaded skill"},
                        ]},
                    ],
                    "tools": base_tools,
                    "max_tokens": 256,
                })
                self.assertIn('"type": "tool_use"', second)
                self.assertIn('"name": "mcp__codebase_memory_mcp__search_graph"', second)
                self.assertIn("Users-sanbo-Desktop-ai_tool_functioncall", second)
                self.assertIn("architecture", second)
                self.assertIn('"stop_reason": "tool_use"', second)
                self.assertNotIn("event: error", second)

                third = run_once({
                    "model": "m",
                    "stream": True,
                    "messages": [
                        {"role": "system", "content": "Available skills:\n- codebase-onboarding"},
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "planner_codebase_onboarding_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "Skill", "input": {"skill": "codebase-onboarding"}},
                        ]},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "planner_codebase_onboarding_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "content": "Successfully loaded skill"},
                        ]},
                        {"role": "assistant", "content": [
                            {
                                "type": "tool_use",
                                "id": "planner_project_structure_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                                "name": "mcp__codebase_memory_mcp__search_graph",
                                "input": {"project": "Users-sanbo-Desktop-ai_tool_functioncall", "query": "project architecture"},
                            },
                        ]},
                        {"role": "user", "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "planner_project_structure_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                                "content": "Architecture: gateway_http_handler -> gateway_tool_runtime",
                            },
                        ]},
                    ],
                    "tools": base_tools,
                    "max_tokens": 256,
                })
                self.assertIn('"type": "tool_use"', third)
                self.assertIn('"name": "mcp__codebase_memory_mcp__search_graph"', third)
                self.assertIn("request flow", third)
                self.assertIn('"stop_reason": "tool_use"', third)
                self.assertNotIn("event: error", third)
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
                if old_project is None:
                    os.environ.pop("GATEWAY_CODEBASE_MEMORY_PROJECT", None)
                else:
                    os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = old_project

    def test_streaming_agent_planner_evidence_survives_context_compaction(self):
        from src.gateway_streaming import _run_streaming_orchestration_scoped

        events = []

        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)

                @staticmethod
                def flush():
                    pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_cwd = os.getcwd()
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            try:
                workspace = pathlib.Path(td) / "workspace"
                workspace.mkdir()
                os.chdir(workspace)
                os.environ["GATEWAY_WORKSPACE_ROOT"] = str(workspace)
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                upstream = FakeClient([{
                    "id": "c_stream",
                    "choices": [{
                        "message": {"role": "assistant", "content": "stream final synthesis"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }])
                body = {
                    "model": "weak",
                    "stream": True,
                    "messages": [
                        {"role": "system", "content": "large streaming harness\n" + ("STREAM-CONTEXT " * 1000)},
                        {"role": "user", "content": "分析这套项目\n" + ("user context " * 1000)},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "bash_1", "name": "Bash", "input": {"command": "find ."}},
                        ]},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "bash_1", "content": "--- files ---\nREADME.md\nsrc/streaming_agent.py\n"},
                        ]},
                    ],
                    "tools": [{
                        "name": "Bash",
                        "input_schema": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                            "additionalProperties": False,
                        },
                    }],
                    "max_tokens": 256,
                }

                _run_streaming_orchestration_scoped(
                    FakeHandler(),
                    "/v1/messages",
                    body,
                    mode="orchestrate",
                    upstream_protocol="openai_chat",
                    gateway_cfg=cfg["gateway"],
                    max_rounds=4,
                    upstream=upstream,
                    context_cfg={
                        "enabled": True,
                        "fanout_enabled": False,
                        "max_input_tokens": 80,
                        "summary_max_chars": 500,
                    },
                )

                self.assertEqual(len(upstream.requests), 1)
                request_body = upstream.requests[0][1]
                full_prompt = json.dumps(request_body, ensure_ascii=False)
                self.assertIn("Gateway Agent Planner evidence", full_prompt)
                self.assertIn("src/streaming_agent.py", full_prompt)
                self.assertNotIn("gateway_context", request_body)
                self.assertNotIn("gateway_agent_planner", request_body)
                text = "".join(events)
                self.assertIn("stream final synthesis", text)
                self.assertIn('"gateway_context"', text)
                self.assertIn('"compacted": true', text)
                self.assertIn("message_stop", text)
                self.assertNotIn("event: error", text)
            finally:
                gateway.CONFIG_PATH = old_config
                os.chdir(old_cwd)
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root

    def test_streaming_agent_planner_synthesizes_after_symbol_deep_dive(self):
        from src.gateway_streaming import _run_streaming_orchestration_scoped
        from src.gateway_agent_planner import list_runtime_events

        events = []

        class FakeHandler:
            class wfile:
                @staticmethod
                def write(data):
                    events.append(data.decode("utf-8") if isinstance(data, bytes) else data)

                @staticmethod
                def flush():
                    pass

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_cwd = os.getcwd()
            old_workspace_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_project = os.environ.get("GATEWAY_CODEBASE_MEMORY_PROJECT")
            old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            planner = None
            try:
                import src.gateway_agent_planner as planner
                planner._STORE = None
                workspace = pathlib.Path(td) / "workspace"
                workspace.mkdir()
                os.chdir(workspace)
                os.environ["GATEWAY_WORKSPACE_ROOT"] = str(workspace)
                os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = "Users-sanbo-Desktop-ai_tool_functioncall"
                os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(workspace)
                cfg["gateway"]["tool_mode"] = "orchestrate"
                cfg["upstream"]["tools_enabled"] = "adapter"
                cfg["upstream"]["protocol"] = "openai_chat"
                cfg["upstream"]["capabilities"]["supports_tools"] = False
                cfg["upstream"]["capabilities"]["supports_function_calls"] = False
                gateway.save_config(cfg)

                upstream = FakeClient([{
                    "id": "c_symbol_stream",
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": (
                                "stream symbol synthesis\n"
                                "```json\n"
                                '{"name":"Edit","arguments":{"file_path":"README.md","old_string":"A","new_string":"B"}}'
                                "\n```"
                            ),
                        },
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }])
                body = {
                    "model": "weak",
                    "stream": True,
                    "metadata": {
                        "session_id": "stream-ignored-upstream-tool-session",
                        "user_id": json.dumps({"user_id": "stream-ignored-upstream-tool-user"}),
                    },
                    "messages": [
                        {"role": "system", "content": "Available skills:\n- codebase-onboarding\n" + ("STREAM-SYMBOL-CONTEXT " * 500)},
                        {"role": "user", "content": "分析这套项目 streaming symbol final\n" + ("user context " * 500)},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "planner_project_structure_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "mcp__codebase_memory_mcp__search_graph", "input": {"project": "Users-sanbo-Desktop-ai_tool_functioncall", "query": "project architecture"}},
                        ]},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "planner_project_structure_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "content": "Architecture evidence"},
                        ]},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "planner_core_flow_trace_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "name": "mcp__codebase_memory_mcp__search_graph", "input": {"project": "Users-sanbo-Desktop-ai_tool_functioncall", "query": "core request flow"}},
                        ]},
                        {"role": "user", "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "planner_core_flow_trace_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                                "content": (
                                    '{"results":[{"qualified_name":'
                                    '"Users-sanbo-Desktop-ai_tool_functioncall.src.gateway_tool_runtime.run_tool_orchestration"}]}'
                                ),
                            },
                        ]},
                        {"role": "assistant", "content": [
                            {
                                "type": "tool_use",
                                "id": "planner_symbol_deep_dive_cccccccccccccccccccccccccccccccc",
                                "name": "mcp__codebase_memory_mcp__get_code_snippet",
                                "input": {
                                    "project": "Users-sanbo-Desktop-ai_tool_functioncall",
                                    "qualified_name": "Users-sanbo-Desktop-ai_tool_functioncall.src.gateway_tool_runtime.run_tool_orchestration",
                                },
                            },
                        ]},
                        {"role": "user", "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "planner_symbol_deep_dive_cccccccccccccccccccccccccccccccc",
                                "content": "def run_tool_orchestration(path, body, client=None):\n    return _run_tool_orchestration_scoped(path, body, client)",
                            },
                        ]},
                    ],
                    "tools": [{
                        "name": "mcp__codebase_memory_mcp__search_graph",
                        "input_schema": {
                            "type": "object",
                            "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                            "required": ["project", "query"],
                            "additionalProperties": False,
                        },
                    }, {
                        "name": "mcp__codebase_memory_mcp__get_code_snippet",
                        "input_schema": {
                            "type": "object",
                            "properties": {"project": {"type": "string"}, "qualified_name": {"type": "string"}},
                            "required": ["project", "qualified_name"],
                            "additionalProperties": False,
                        },
                    }],
                    "max_tokens": 256,
                }

                _run_streaming_orchestration_scoped(
                    FakeHandler(),
                    "/v1/messages",
                    body,
                    mode="orchestrate",
                    upstream_protocol="openai_chat",
                    gateway_cfg=cfg["gateway"],
                    max_rounds=4,
                    upstream=upstream,
                    context_cfg={
                        "enabled": True,
                        "fanout_enabled": False,
                        "max_input_tokens": 120,
                        "summary_max_chars": 700,
                    },
                )

                self.assertEqual(len(upstream.requests), 1)
                request_body = upstream.requests[0][1]
                full_prompt = json.dumps(request_body, ensure_ascii=False)
                self.assertIn("Gateway Agent Planner evidence", full_prompt)
                self.assertIn("run_tool_orchestration", full_prompt)
                self.assertIn("symbol_deep_dive", full_prompt)
                self.assertNotIn("tools", request_body)
                self.assertNotIn("tool_choice", request_body)
                self.assertNotIn("gateway_context", request_body)
                self.assertNotIn("gateway_agent_planner", request_body)
                text = "".join(events)
                self.assertIn("stream symbol synthesis", text)
                self.assertIn('"gateway_context"', text)
                self.assertIn('"chat_only_synthesis": true', text)
                self.assertIn('"upstream_tools_stripped": true', text)
                self.assertIn("message_stop", text)
                self.assertNotIn('"stop_reason": "tool_use"', text)
                self.assertNotIn("event: error", text)
                boundary_events = list_runtime_events(
                    10,
                    tenant_contains="stream-ignored-upstream-tool-user",
                    event_type="chat_only_synthesis_boundary",
                )
                self.assertEqual(len(boundary_events), 1)
                self.assertEqual(boundary_events[0]["workflow"], "chat_only_synthesis")
                self.assertEqual(boundary_events[0]["step"], "strip_upstream_tools")
                self.assertEqual(boundary_events[0]["metadata"]["source"], "streaming")
                self.assertFalse(boundary_events[0]["metadata"]["tool_authority_granted"])
                runtime_events = list_runtime_events(
                    10,
                    tenant_contains="stream-ignored-upstream-tool-user",
                    event_type="upstream_tool_attempt_ignored",
                )
                self.assertEqual(len(runtime_events), 1)
                self.assertEqual(runtime_events[0]["metadata"]["source"], "streaming")
                self.assertEqual(runtime_events[0]["metadata"]["calls"][0]["name"], "Edit")
                self.assertFalse(runtime_events[0]["metadata"]["tool_authority_granted"])
            finally:
                gateway.CONFIG_PATH = old_config
                os.chdir(old_cwd)
                if old_workspace_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_workspace_root
                if old_project is None:
                    os.environ.pop("GATEWAY_CODEBASE_MEMORY_PROJECT", None)
                else:
                    os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = old_project
                if old_runtime is None:
                    os.environ.pop("GATEWAY_RUNTIME_DIR", None)
                else:
                    os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
                if planner is not None:
                    planner._STORE = None

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

    def test_stream_final_response_carries_gateway_context_metadata(self):
        from src.gateway_streaming import _stream_final_response

        def collect(path, response):
            events = []

            class FakeHandler:
                class wfile:
                    @staticmethod
                    def write(data):
                        events.append(data.decode("utf-8") if isinstance(data, bytes) else data)

                    @staticmethod
                    def flush():
                        pass

            _stream_final_response(FakeHandler(), path, response)
            return "".join(events)

        context = {
            "agent_planner": {"workflow": "project_analysis", "step": "final_synthesis"},
            "strategy": "agent_planner_final_synthesis",
            "planner_evidence_chars": 1234,
        }
        chat_stream = collect(
            "/v1/chat/completions",
            {
                "id": "chat_ctx",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "gateway_context": context,
            },
        )
        self.assertIn('"gateway_context"', chat_stream)
        self.assertIn('"final_synthesis"', chat_stream)

        messages_stream = collect(
            "/v1/messages",
            {
                "id": "msg_ctx",
                "model": "m",
                "content": [{"type": "text", "text": "ok"}],
                "gateway_context": context,
            },
        )
        self.assertIn('"gateway_context"', messages_stream)
        self.assertIn('"agent_planner"', messages_stream)

        responses_stream = collect(
            "/v1/responses",
            {
                "id": "resp_ctx",
                "object": "response",
                "model": "m",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
                "gateway_context": context,
            },
        )
        self.assertIn('"gateway_context"', responses_stream)
        self.assertIn('"planner_evidence_chars": 1234', responses_stream)


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

    def test_detect_openai_legacy_function_call_delta(self):
        """OpenAI legacy function_call delta fragment → call_id + name + args."""
        line = 'data: {"id":"c","choices":[{"delta":{"function_call":{"name":"calculator","arguments":"{\\"expression\\":\\"6*7\\"}"}}}]})'
        _, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/chat/completions", None, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "calculator")
        self.assertIn("6*7", calls[0]["arguments"])

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

    def test_detect_responses_custom_tool_call_item(self):
        """Responses custom_tool_call streaming item → parsed like non-streaming Responses."""
        line = 'event: response.output_item.done\ndata: {"type":"response.output_item.done","output_index":0,"item":{"type":"custom_tool_call","id":"ctc_abc","call_id":"call_custom","name":"calculator","input":"40+2"}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/responses", event, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "call_custom")
        self.assertEqual(calls[0]["name"], "calculator")
        self.assertEqual(calls[0]["arguments"], '{"input": "40+2"}')

    def test_detect_responses_codex_builtin_tool_call_item(self):
        """Responses local_shell_call streaming item → parsed as a programmable tool call."""
        line = 'event: response.output_item.done\ndata: {"type":"response.output_item.done","output_index":0,"item":{"type":"local_shell_call","id":"lsc_abc","call_id":"call_shell","action":{"command":"pwd"}}}'
        event, data = gateway._parse_sse_line(line)
        calls = gateway._detect_streaming_tool_calls_from_sse("/v1/responses", event, data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "call_shell")
        self.assertEqual(calls[0]["name"], "local_shell")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"command": "pwd"})

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

    def test_anthropic_tool_result_error_roundtrips_through_chat_marker(self):
        from src.gateway_protocol import _convert_anthropic_messages_to_openai, _openai_messages_to_anthropic

        messages = [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_error",
                "content": "permission denied",
                "is_error": True,
            }],
        }]

        chat_messages, _, _ = _convert_anthropic_messages_to_openai(messages)
        self.assertEqual(chat_messages[0]["role"], "tool")
        self.assertIn("[gateway_tool_result_error]", chat_messages[0]["content"])

        anthropic_messages, _ = _openai_messages_to_anthropic(chat_messages)
        tool_result = anthropic_messages[0]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "toolu_error")
        self.assertEqual(tool_result["content"], "permission denied")
        self.assertTrue(tool_result["is_error"])

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

    def test_openai_chat_legacy_function_call_response_to_anthropic_tool_use(self):
        from src.gateway_protocol import _from_openai_chat_response

        result = _from_openai_chat_response("/v1/messages", {
            "id": "chatcmpl_legacy_fn",
            "model": "weak",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "function_call": {
                        "name": "read_file",
                        "arguments": "{\"path\":\"README.md\"}",
                    },
                },
                "finish_reason": "function_call",
            }],
        })

        self.assertEqual(result["stop_reason"], "tool_use")
        tool_use = [block for block in result["content"] if block.get("type") == "tool_use"]
        self.assertEqual(len(tool_use), 1)
        self.assertEqual(tool_use[0]["name"], "read_file")
        self.assertEqual(tool_use[0]["input"], {"path": "README.md"})

    def test_openai_chat_tool_call_non_object_arguments_wrap_for_anthropic(self):
        from src.gateway_protocol import _from_openai_chat_response

        result = _from_openai_chat_response("/v1/messages", {
            "id": "chatcmpl_custom_args",
            "model": "weak",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_custom",
                        "type": "function",
                        "function": {
                            "name": "custom_text_tool",
                            "arguments": "search README for setup",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        })

        tool_use = [block for block in result["content"] if block.get("type") == "tool_use"]
        self.assertEqual(tool_use[0]["input"], {"input": "search README for setup"})

    def test_openai_chat_legacy_function_history_converts_to_responses(self):
        from src.gateway_protocol import _openai_chat_to_responses_payload

        converted = _openai_chat_to_responses_payload({
            "model": "weak",
            "messages": [
                {"role": "user", "content": "calc"},
                {
                    "role": "assistant",
                    "content": None,
                    "function_call": {
                        "name": "calculator",
                        "arguments": "{\"expression\":\"6*7\"}",
                    },
                },
                {"role": "function", "name": "calculator", "content": "42"},
            ],
        })

        function_call = converted["input"][1]
        function_output = converted["input"][2]
        self.assertEqual(function_call["type"], "function_call")
        self.assertEqual(function_call["name"], "calculator")
        self.assertEqual(function_output["type"], "function_call_output")
        self.assertEqual(function_output["call_id"], function_call["call_id"])
        self.assertEqual(function_output["output"], "42")

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

    def test_compact_request_for_upstream_injects_periodic_summary(self):
        """Long chat history should become summary + recent messages, not raw truncation only."""
        from src.gateway_context import _compact_request_for_upstream
        body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "old user detail " * 200},
                {"role": "assistant", "content": "old assistant detail " * 200},
                {"role": "user", "content": "recent question"},
                {"role": "assistant", "content": "recent answer"},
            ],
        }
        with patch("src.gateway_context._summarize_via_llm", return_value="old-turn-summary"):
            result = _compact_request_for_upstream(
                "/v1/chat/completions",
                body,
                {"keep_recent_messages": 2, "summary_max_chars": 500},
                reason="over_limit",
            )
        messages = result["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("[Previous conversation summary]", messages[0]["content"])
        self.assertIn("old-turn-summary", messages[0]["content"])
        self.assertEqual(messages[-2]["content"], "recent question")
        self.assertEqual(messages[-1]["content"], "recent answer")
        self.assertNotIn("old user detail " * 20, json.dumps(messages, ensure_ascii=False))

    def test_messages_compaction_moves_summary_into_system_field(self):
        """Anthropic messages should not receive a synthetic system role message."""
        from src.gateway_context import _compact_request_for_upstream
        body = {
            "model": "m",
            "system": "original system",
            "messages": [
                {"role": "user", "content": "old user detail " * 200},
                {"role": "assistant", "content": "old assistant detail " * 200},
                {"role": "user", "content": "recent question"},
            ],
        }
        with patch("src.gateway_context._summarize_via_llm", return_value="anthropic-old-summary"):
            result = _compact_request_for_upstream(
                "/v1/messages",
                body,
                {"keep_recent_messages": 1, "summary_max_chars": 500},
                reason="over_limit",
            )
        self.assertIn("[gateway context compacted]", result["system"])
        self.assertIn("original system", result["system"])
        self.assertIn("anthropic-old-summary", result["system"])
        self.assertEqual(result["messages"], [{"role": "user", "content": "recent question"}])
        self.assertFalse(any(m.get("role") == "system" for m in result["messages"]))

    def test_responses_input_list_compaction_trims_large_recent_item_content(self):
        """Responses current input item must be shrunk for tiny upstream windows."""
        from src.gateway_context import _compact_request_for_upstream
        huge_recent = "recent responses payload " * 500
        body = {
            "model": "m",
            "input": [
                {"role": "user", "content": "old user detail " * 200},
                {"role": "assistant", "content": "old assistant detail " * 200},
                {"role": "user", "content": huge_recent},
            ],
        }
        with patch("src.gateway_context._summarize_via_llm", return_value="responses-old-summary"):
            result = _compact_request_for_upstream(
                "/v1/responses",
                body,
                {"keep_recent_messages": 1, "summary_max_chars": 500},
                reason="over_limit",
            )
        recent = result["input"][-1]
        self.assertEqual(recent["role"], "user")
        self.assertIn("...(truncated)", recent["content"])
        self.assertLess(len(recent["content"]), len(huge_recent))
        self.assertNotIn("recent responses payload " * 100, json.dumps(result, ensure_ascii=False))

    def test_responses_input_list_compaction_keeps_summary_and_recent_items(self):
        """Responses input arrays should also get rolling summary compaction."""
        from src.gateway_context import _compact_request_for_upstream
        body = {
            "model": "m",
            "input": [
                {"role": "user", "content": "old user detail " * 200},
                {"role": "assistant", "content": "old assistant detail " * 200},
                {"role": "user", "content": "recent question"},
            ],
        }
        with patch("src.gateway_context._summarize_via_llm", return_value="responses-old-summary"):
            result = _compact_request_for_upstream(
                "/v1/responses",
                body,
                {"keep_recent_messages": 1, "summary_max_chars": 500},
                reason="over_limit",
            )
        self.assertIn("[gateway context compacted]", result["instructions"])
        self.assertEqual(result["input"][0]["role"], "system")
        self.assertIn("responses-old-summary", result["input"][0]["content"])
        self.assertEqual(result["input"][-1], {"role": "user", "content": "recent question"})


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
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        old_strict = os.environ.get("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = "1"
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
            body = dict(body)
            metadata = dict(body.get("metadata") or {})
            metadata.setdefault("workspace", td)
            body["metadata"] = metadata
            result = run_tool_orchestration(path, body, client)
            return result
        finally:
            gateway.CONFIG_PATH = old_config
            os.chdir(old_cwd)
            if planner is not None:
                planner._STORE = None
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws
            if old_runtime is None:
                os.environ.pop("GATEWAY_RUNTIME_DIR", None)
            else:
                os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
            if old_strict is None:
                os.environ.pop("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN", None)
            else:
                os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = old_strict

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

    def test_direct_downstream_fallback_records_remote_runtime_event(self):
        from src.gateway_agent_planner import list_runtime_events

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["gateway"]["agent_planner_strict_every_turn"] = True
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            gateway.save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")

            client = FakeClient([])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "metadata": {
                    "session_id": "remote-project-session",
                    "user_id": json.dumps({"user_id": "remote-project-user"}),
                },
                "messages": [{"role": "user", "content": "分析这套项目"}],
                "max_tokens": 4096,
            }, client)

            self.assertEqual(result.get("stop_reason"), "tool_use")
            self.assertEqual(client.requests, [])
            events = list_runtime_events(
                10,
                tenant_contains="remote-project-user",
                workflow="direct_downstream_tool_request",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "tool_dispatch")
            self.assertEqual(events[0]["step"], "surface_user_side_tools")
            self.assertIn(events[0]["metadata"]["calls"][0]["name"], {"LS", "Glob"})
            self.assertIn("remote-project-session", events[0]["session_key"])
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
            if planner is not None:
                planner._STORE = None

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

    def test_chat_only_project_analysis_without_path_surfaces_native_tool_fanout_before_upstream(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        old_strict = os.environ.get("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = "1"
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["gateway"]["agent_planner_strict_every_turn"] = True
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            client = FakeClient([])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "messages": [{"role": "user", "content": "分析这套项目"}],
                "max_tokens": 4096,
            }, client)

            self.assertEqual(client.requests, [], "chat-only upstream must not be asked to see local files before tools run")
            self.assertEqual(result.get("stop_reason"), "tool_use")
            self.assertEqual((result.get("gateway_context") or {}).get("strategy"), "gateway_downstream_tool_request")
            tool_names = [b.get("name") for b in (result.get("content") or []) if b.get("type") == "tool_use"]
            self.assertIn("LS", tool_names)
            self.assertIn("Glob", tool_names)
        finally:
            gateway.CONFIG_PATH = old_config
            os.chdir(old_cwd)
            if planner is not None:
                planner._STORE = None
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws
            if old_runtime is None:
                os.environ.pop("GATEWAY_RUNTIME_DIR", None)
            else:
                os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
            if old_strict is None:
                os.environ.pop("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN", None)
            else:
                os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = old_strict

    def test_chat_only_project_analysis_uses_declared_shell_tool_when_ls_glob_absent(self):
        result = self._run("/v1/responses", {
            "model": "weak",
            "input": "分析这套项目",
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            }],
        })

        calls = [o for o in (result.get("output") or []) if o.get("type") == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("name"), "exec_command")
        args = json.loads(calls[0].get("arguments") or "{}")
        self.assertIn("find", args.get("cmd", ""))
        self.assertNotIn("command", args)

    def test_chat_only_project_analysis_prefers_codebase_onboarding_skill_when_available(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [
                {"role": "system", "content": "Available skills:\\n- codebase-onboarding\\n- codebase-memory"},
                {"role": "user", "content": "分析这套项目"},
            ],
            "tools": [{
                "name": "Skill",
                "input_schema": {
                    "type": "object",
                    "properties": {"skill": {"type": "string"}, "args": {"type": "string"}},
                    "required": ["skill"],
                    "additionalProperties": False,
                },
            }],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "Skill")
        self.assertEqual(tool_use[0].get("input"), {"skill": "codebase-onboarding"})
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "codebase_onboarding")

    def test_agent_planner_emits_update_plan_before_project_tools_when_declared(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [
                {"role": "system", "content": "Available skills:\\n- codebase-onboarding"},
                {"role": "user", "content": "分析这套项目"},
            ],
            "tools": [
                {
                    "name": "update_plan",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "plan": {"type": "array"},
                            "explanation": {"type": "string"},
                        },
                        "required": ["plan"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "Skill",
                    "input_schema": {
                        "type": "object",
                        "properties": {"skill": {"type": "string"}, "args": {"type": "string"}},
                        "required": ["skill"],
                        "additionalProperties": False,
                    },
                },
            ],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "update_plan")
        plan = tool_use[0].get("input", {}).get("plan") or []
        self.assertEqual(plan[0].get("status"), "in_progress")
        self.assertIn("项目", plan[0].get("step", ""))
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "planner_progress")

    def test_agent_planner_continues_to_skill_after_update_plan_result(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [
                {"role": "system", "content": "Available skills:\\n- codebase-onboarding"},
                {"role": "user", "content": "分析这套项目"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "plan_1", "name": "update_plan", "input": {
                        "plan": [{"step": "加载项目分析技能/上下文规则", "status": "in_progress"}],
                    }},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "plan_1", "content": "{\"ok\":true}"},
                ]},
            ],
            "tools": [
                {
                    "name": "update_plan",
                    "input_schema": {
                        "type": "object",
                        "properties": {"plan": {"type": "array"}},
                        "required": ["plan"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "Skill",
                    "input_schema": {
                        "type": "object",
                        "properties": {"skill": {"type": "string"}, "args": {"type": "string"}},
                        "required": ["skill"],
                        "additionalProperties": False,
                    },
                },
            ],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "Skill")
        self.assertEqual(tool_use[0].get("input"), {"skill": "codebase-onboarding"})
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "codebase_onboarding")

    def test_agent_planner_continues_after_codebase_onboarding_skill_result(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [
                {"role": "system", "content": "Available skills:\\n- codebase-onboarding"},
                {"role": "user", "content": "分析这套项目"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "skill_1", "name": "Skill", "input": {"skill": "codebase-onboarding"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "skill_1", "content": "Successfully loaded skill"},
                ]},
            ],
            "tools": [{
                "name": "Bash",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            }],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "Bash")
        self.assertIn("find", tool_use[0].get("input", {}).get("command", ""))
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "project_structure")
        state = (result.get("gateway_context") or {}).get("agent_planner", {}).get("state") or {}
        self.assertEqual(state.get("workflow"), "project_analysis")
        self.assertEqual(state.get("current_step"), "project_structure")
        self.assertGreaterEqual(state.get("evidence_count", 0), 1)

    def test_agent_planner_prefers_codebase_search_graph_for_project_structure(self):
        old_project = os.environ.get("GATEWAY_CODEBASE_MEMORY_PROJECT")
        try:
            os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = "Users-sanbo-Desktop-ai_tool_functioncall"
            result = self._run("/v1/messages", {
                "model": "weak",
                "metadata": {"session_id": "synthesis-boundary-session", "user_id": json.dumps({"user_id": "synthesis-boundary-user"})},
                "messages": [
                    {"role": "system", "content": "Available skills:\\n- codebase-onboarding"},
                    {"role": "user", "content": "分析这套项目"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "skill_1", "name": "Skill", "input": {"skill": "codebase-onboarding"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "skill_1", "content": "Successfully loaded skill"},
                    ]},
                ],
                "tools": [{
                    "name": "mcp__codebase_memory_mcp__search_graph",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string"},
                            "query": {"type": "string"},
                        },
                        "required": ["project", "query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "Bash",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            })
        finally:
            if old_project is None:
                os.environ.pop("GATEWAY_CODEBASE_MEMORY_PROJECT", None)
            else:
                os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = old_project

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "mcp__codebase_memory_mcp__search_graph")
        self.assertEqual(tool_use[0].get("input", {}).get("project"), "Users-sanbo-Desktop-ai_tool_functioncall")
        self.assertIn("architecture", tool_use[0].get("input", {}).get("query", ""))
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "project_structure")

    def test_agent_planner_traces_core_flow_after_planner_structure_step(self):
        old_project = os.environ.get("GATEWAY_CODEBASE_MEMORY_PROJECT")
        try:
            os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = "Users-sanbo-Desktop-ai_tool_functioncall"
            result = self._run("/v1/messages", {
                "model": "weak",
                "messages": [
                    {"role": "system", "content": "Available skills:\n- codebase-onboarding"},
                    {"role": "user", "content": "分析这套项目"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "skill_1", "name": "Skill", "input": {"skill": "codebase-onboarding"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "skill_1", "content": "Successfully loaded skill"},
                    ]},
                    {"role": "assistant", "content": [
                        {
                            "type": "tool_use",
                            "id": "planner_project_structure_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "name": "mcp__codebase_memory_mcp__get_architecture",
                            "input": {"project": "Users-sanbo-Desktop-ai_tool_functioncall"},
                        },
                    ]},
                    {"role": "user", "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "planner_project_structure_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "content": "Architecture: src/gateway_http_handler.py routes requests into src/gateway_tool_runtime.py",
                        },
                    ]},
                ],
                "tools": [{
                    "name": "mcp__codebase_memory_mcp__search_graph",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string"},
                            "query": {"type": "string"},
                        },
                        "required": ["project", "query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "Read",
                    "input_schema": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            })
        finally:
            if old_project is None:
                os.environ.pop("GATEWAY_CODEBASE_MEMORY_PROJECT", None)
            else:
                os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = old_project

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "mcp__codebase_memory_mcp__search_graph")
        self.assertEqual(tool_use[0].get("input", {}).get("project"), "Users-sanbo-Desktop-ai_tool_functioncall")
        self.assertIn("request flow", tool_use[0].get("input", {}).get("query", ""))
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "core_flow_trace")
        state = (result.get("gateway_context") or {}).get("agent_planner", {}).get("state") or {}
        self.assertIn("project_structure", state.get("completed_steps") or [])
        self.assertEqual(state.get("current_step"), "core_flow_trace")

    def test_agent_planner_deep_dives_symbol_after_core_flow_trace(self):
        old_project = os.environ.get("GATEWAY_CODEBASE_MEMORY_PROJECT")
        try:
            os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = "Users-sanbo-Desktop-ai_tool_functioncall"
            result = self._run("/v1/messages", {
                "model": "weak",
                "messages": [
                    {"role": "user", "content": "分析这套项目"},
                    {"role": "assistant", "content": [
                        {
                            "type": "tool_use",
                            "id": "planner_project_structure_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "name": "mcp__codebase_memory_mcp__get_architecture",
                            "input": {"project": "Users-sanbo-Desktop-ai_tool_functioncall"},
                        },
                    ]},
                    {"role": "user", "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "planner_project_structure_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "content": "Architecture evidence",
                        },
                    ]},
                    {"role": "assistant", "content": [
                        {
                            "type": "tool_use",
                            "id": "planner_core_flow_trace_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                            "name": "mcp__codebase_memory_mcp__search_graph",
                            "input": {
                                "project": "Users-sanbo-Desktop-ai_tool_functioncall",
                                "query": "core request flow",
                            },
                        },
                    ]},
                    {"role": "user", "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "planner_core_flow_trace_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                            "content": (
                                '{"results":[{"qualified_name":'
                                '"Users-sanbo-Desktop-ai_tool_functioncall.src.gateway_tool_runtime.run_tool_orchestration",'
                                '"name":"run_tool_orchestration"}]}'
                            ),
                        },
                    ]},
                ],
                "tools": [{
                    "name": "mcp__codebase_memory_mcp__get_code_snippet",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string"},
                            "qualified_name": {"type": "string"},
                            "include_neighbors": {"type": "boolean"},
                        },
                        "required": ["project", "qualified_name"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "mcp__codebase_memory_mcp__trace_path",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string"},
                            "function_name": {"type": "string"},
                            "direction": {"type": "string"},
                            "mode": {"type": "string"},
                            "depth": {"type": "integer"},
                        },
                        "required": ["project", "function_name"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "Read",
                    "input_schema": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            })
        finally:
            if old_project is None:
                os.environ.pop("GATEWAY_CODEBASE_MEMORY_PROJECT", None)
            else:
                os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = old_project

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        names = [b.get("name") for b in tool_use]
        self.assertIn("mcp__codebase_memory_mcp__get_code_snippet", names)
        self.assertIn("mcp__codebase_memory_mcp__trace_path", names)
        snippet = next(b for b in tool_use if b.get("name") == "mcp__codebase_memory_mcp__get_code_snippet")
        self.assertEqual(snippet.get("input", {}).get("project"), "Users-sanbo-Desktop-ai_tool_functioncall")
        self.assertEqual(
            snippet.get("input", {}).get("qualified_name"),
            "Users-sanbo-Desktop-ai_tool_functioncall.src.gateway_tool_runtime.run_tool_orchestration",
        )
        trace = next(b for b in tool_use if b.get("name") == "mcp__codebase_memory_mcp__trace_path")
        self.assertEqual(trace.get("input", {}).get("function_name"), "run_tool_orchestration")
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "symbol_deep_dive")

    def test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            weak_resp = {"id": "c1", "choices": [{"message": {"role": "assistant", "content": "final analysis"},
                                                   "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            client = FakeClient([weak_resp])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "metadata": {"session_id": "synthesis-boundary-session", "user_id": json.dumps({"user_id": "synthesis-boundary-user"})},
                "messages": [
                    {"role": "user", "content": "分析这套项目"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "bash_1", "name": "Bash", "input": {"command": "find ."}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "bash_1", "content": "--- files ---\nREADME.md\nsrc/gateway_tool_runtime.py"},
                    ]},
                ],
                "tools": [{
                    "name": "Bash",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            }, client)

            self.assertEqual(result.get("content", [{}])[0].get("text"), "final analysis")
            self.assertEqual(len(client.requests), 1)
            upstream_body = client.requests[0][1]
            system_text = (upstream_body.get("messages") or [{}])[0].get("content", "")
            self.assertIn("Gateway Agent Planner evidence", system_text)
            self.assertIn("src/gateway_tool_runtime.py", system_text)
            self.assertNotIn("tools", upstream_body)
            self.assertNotIn("tool_choice", upstream_body)
            self.assertNotIn("gateway_context", upstream_body)
            self.assertNotIn("gateway_agent_planner", upstream_body)
            final_ctx = result.get("gateway_context") or {}
            self.assertEqual(final_ctx.get("strategy"), "agent_planner_final_synthesis")
            self.assertTrue(final_ctx.get("chat_only_synthesis"))
            self.assertTrue(final_ctx.get("upstream_tools_stripped"))
            state = (final_ctx.get("agent_planner") or {}).get("state") or {}
            self.assertEqual(state.get("current_step"), "synthesis")
            self.assertGreaterEqual(state.get("evidence_summary_chars", 0), len("src/gateway_tool_runtime.py"))
            self.assertIn("src/gateway_tool_runtime.py", state.get("evidence_summary_preview", ""))
            from src.gateway_agent_planner import list_runtime_events
            events = list_runtime_events(
                20,
                tenant_contains="synthesis-boundary-user",
                event_type="chat_only_synthesis_boundary",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["workflow"], "chat_only_synthesis")
            self.assertEqual(events[0]["step"], "strip_upstream_tools")
            self.assertFalse(events[0]["metadata"]["tool_authority_granted"])
            self.assertTrue(events[0]["metadata"]["upstream_tools_stripped"])
            self.assertIn("synthesis-boundary-session", events[0]["session_key"])
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
            if planner is not None:
                planner._STORE = None

    def test_agent_planner_does_not_leak_chat_only_refusal_after_evidence(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            weak_refusal = {
                "id": "c_refusal",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Hello, I can't answer this question for now. Let's talk about something else.",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            client = FakeClient([weak_refusal])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "metadata": {"session_id": "refusal-fallback-session", "user_id": json.dumps({"user_id": "refusal-fallback-user"})},
                "messages": [
                    {"role": "user", "content": "分析这套项目"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "bash_1", "name": "Bash", "input": {"command": "find ."}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "bash_1", "content": "--- files ---\nREADME.md\nsrc/gateway_tool_runtime.py"},
                    ]},
                ],
                "tools": [{
                    "name": "Bash",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            }, client)

            text = result.get("content", [{}])[0].get("text", "")
            self.assertNotIn("Let's talk about something else", text)
            self.assertIn("Gateway 已改用 planner 证据生成兜底结论", text)
            self.assertIn("README.md", text)
            self.assertTrue((result.get("gateway_agent_planner") or {}).get("synthesis_refusal_fallback"))
            self.assertFalse((result.get("gateway_agent_planner") or {}).get("synthesis_scope_fallback"))
            self.assertFalse((result.get("gateway_agent_planner") or {}).get("synthesis_nonanswer_fallback"))
            self.assertEqual((result.get("gateway_context") or {}).get("strategy"), "agent_planner_final_synthesis")
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
            if planner is not None:
                planner._STORE = None

    def test_agent_planner_does_not_leak_cross_session_path_drift_after_evidence(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            weak_drift = {
                "id": "c_scope_drift",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": (
                            "根据上一个 session 的记录，你的项目是 `chatgpt2api`，位于 "
                            "`/Users/sanbo/Desktop/api/chatgpt2api`，让我用正确的路径来分析。"
                        ),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            client = FakeClient([weak_drift])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "metadata": {"session_id": "scope-drift-session", "user_id": json.dumps({"user_id": "scope-drift-user"})},
                "messages": [
                    {"role": "user", "content": "分析这套项目"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "readme_1", "name": "Read", "input": {"file_path": "README.md"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "readme_1", "content": "# Current Gateway\nsrc/gateway_tool_runtime.py"},
                    ]},
                ],
                "tools": [{
                    "name": "Read",
                    "input_schema": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            }, client)

            text = result.get("content", [{}])[0].get("text", "")
            self.assertNotIn("/Users/sanbo/Desktop/api/chatgpt2api", text)
            self.assertIn("Gateway 已改用 planner 证据生成兜底结论", text)
            self.assertIn("Current Gateway", text)
            self.assertFalse((result.get("gateway_agent_planner") or {}).get("synthesis_refusal_fallback"))
            self.assertTrue((result.get("gateway_agent_planner") or {}).get("synthesis_scope_fallback"))
            self.assertFalse((result.get("gateway_agent_planner") or {}).get("synthesis_nonanswer_fallback"))
            self.assertEqual((result.get("gateway_context") or {}).get("strategy"), "agent_planner_final_synthesis")
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
            if planner is not None:
                planner._STORE = None

    def test_agent_planner_does_not_leak_final_synthesis_nonanswer_after_evidence(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            weak_nonanswer = {
                "id": "c_nonanswer",
                "choices": [{
                    "message": {"role": "assistant", "content": "Let me first see what's actually in that directory."},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            client = FakeClient([weak_nonanswer])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "metadata": {"session_id": "nonanswer-session", "user_id": json.dumps({"user_id": "nonanswer-user"})},
                "messages": [
                    {"role": "user", "content": "分析这套项目"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "bash_1", "name": "Bash", "input": {"command": "find ."}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "bash_1", "content": "--- files ---\nREADME.md\nsrc/gateway_agent_planner.py"},
                    ]},
                ],
                "tools": [{
                    "name": "Bash",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            }, client)

            text = result.get("content", [{}])[0].get("text", "")
            self.assertNotIn("Let me first", text)
            self.assertIn("Gateway 已改用 planner 证据生成兜底结论", text)
            self.assertIn("src/gateway_agent_planner.py", text)
            self.assertFalse((result.get("gateway_agent_planner") or {}).get("synthesis_refusal_fallback"))
            self.assertFalse((result.get("gateway_agent_planner") or {}).get("synthesis_scope_fallback"))
            self.assertTrue((result.get("gateway_agent_planner") or {}).get("synthesis_nonanswer_fallback"))
            self.assertEqual((result.get("gateway_context") or {}).get("strategy"), "agent_planner_final_synthesis")
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
            if planner is not None:
                planner._STORE = None

    def test_agent_planner_ignores_client_injected_user_reminders_for_intent(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        old_strict = os.environ.get("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = "1"
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["gateway"]["agent_planner_strict_every_turn"] = True
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            client = FakeClient([{
                "id": "c_plain",
                "choices": [{
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "metadata": {"session_id": "injected-reminder-session", "user_id": "injected-reminder-user"},
                "messages": [
                    {"role": "user", "content": [{
                        "type": "text",
                        "text": (
                            "<system-reminder>\n"
                            "As you answer the user's questions, you can use the following context:\n"
                            "# claudeMd\n"
                            "Run lint, typecheck, tests, and static analysis after changes.\n"
                        ),
                    }]},
                    {"role": "system", "content": "SessionStart:startup hook success: Previous session summary: run tests"},
                ],
                "tools": [{
                    "name": "Bash",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 128,
            }, client)

            self.assertEqual(result.get("stop_reason"), "end_turn")
            text = result.get("content", [{}])[0].get("text", "")
            self.assertEqual(text, "ok")
            self.assertEqual(len(client.requests), 1)
            ctx = result.get("gateway_context") or {}
            intent = (((ctx.get("agent_planner") or {}).get("intent")) or {})
            self.assertEqual(intent.get("kind"), "plain_chat")
            self.assertEqual(intent.get("workflow"), "chat_only_synthesis")
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
            if old_strict is None:
                os.environ.pop("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN", None)
            else:
                os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = old_strict
            if planner is not None:
                planner._STORE = None

    def test_plain_chat_is_wrapped_by_agent_planner_envelope(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration
        from src.gateway_agent_planner import list_runtime_events

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        old_strict = os.environ.get("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = "1"
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["gateway"]["agent_planner_strict_every_turn"] = True
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            weak_resp = {"id": "plain", "choices": [{"message": {"role": "assistant", "content": "hi there"},
                                                       "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            client = FakeClient([weak_resp])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "metadata": {"session_id": "plain-planner-session", "user_id": json.dumps({"user_id": "plain-planner-user"})},
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{
                    "name": "Bash",
                    "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
                }],
                "max_tokens": 4096,
            }, client)

            self.assertEqual(result.get("content", [{}])[0].get("text"), "hi there")
            ctx = result.get("gateway_context") or {}
            self.assertEqual(ctx.get("strategy"), "agent_planner_final_synthesis")
            self.assertTrue(ctx.get("chat_only_synthesis"))
            planner_ctx = ctx.get("agent_planner") or {}
            self.assertEqual(planner_ctx.get("workflow"), "chat_only_synthesis")
            self.assertEqual((planner_ctx.get("intent") or {}).get("kind"), "plain_chat")
            upstream_body = client.requests[0][1]
            self.assertNotIn("tools", upstream_body)
            system_text = (upstream_body.get("messages") or [{}])[0].get("content", "")
            self.assertIn("Gateway Agent Planner evidence/envelope", system_text)
            self.assertIn('"kind": "plain_chat"', system_text)
            events = list_runtime_events(20, tenant_contains="plain-planner-user")
            event_types = [event.get("event_type") for event in events]
            self.assertIn("intent_classification", event_types)
            self.assertIn("chat_only_synthesis_boundary", event_types)
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
            if old_strict is None:
                os.environ.pop("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN", None)
            else:
                os.environ["GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN"] = old_strict
            if planner is not None:
                planner._STORE = None

    def test_chat_only_final_synthesis_ignores_upstream_json_tool_request(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration
        from src.gateway_agent_planner import list_runtime_events

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")

            fake_tool_json = '{"name":"Edit","arguments":{"file_path":"README.md","old_string":"A","new_string":"B"}}'
            client = FakeClient([{
                "id": "c1",
                "choices": [{"message": {"role": "assistant", "content": fake_tool_json}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "metadata": {
                    "session_id": "ignored-upstream-tool-session",
                    "user_id": json.dumps({"user_id": "ignored-upstream-tool-user"}),
                },
                "messages": [
                    {"role": "user", "content": "分析这套项目"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "read_1", "name": "Read", "input": {"file_path": "README.md"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "read_1", "content": "# TestProject\nA"},
                    ]},
                ],
                "tools": [{
                    "name": "Edit",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                        },
                        "required": ["file_path", "old_string", "new_string"],
                    },
                }],
                "max_tokens": 4096,
            }, client)

            self.assertEqual(len(client.requests), 1)
            upstream_body = client.requests[0][1]
            self.assertNotIn("tools", upstream_body)
            self.assertNotIn("gateway_context", upstream_body)
            self.assertNotIn("gateway_agent_planner", upstream_body)
            content = result.get("content") or []
            self.assertEqual(content[0].get("type"), "text")
            self.assertIn('"name":"Edit"', content[0].get("text", ""))
            self.assertFalse([block for block in content if block.get("type") == "tool_use"])
            self.assertTrue((result.get("gateway_context") or {}).get("chat_only_synthesis"))
            events = list_runtime_events(
                10,
                tenant_contains="ignored-upstream-tool-user",
                event_type="upstream_tool_attempt_ignored",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["workflow"], "chat_only_synthesis")
            self.assertEqual(events[0]["step"], "ignore_upstream_tool_attempt")
            self.assertFalse(events[0]["metadata"]["tool_authority_granted"])
            self.assertEqual(events[0]["metadata"]["calls"][0]["name"], "Edit")
            self.assertIn("ignored-upstream-tool-session", events[0]["session_key"])
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
            if planner is not None:
                planner._STORE = None

    def test_intent_detection_does_not_treat_files_to_as_ls_tool(self):
        from src.gateway_tool_runtime import _detect_intent_tool_calls

        body = {
            "model": "weak",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        }
        response = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Let me gather the project structure and key files to analyze this project."}],
            "stop_reason": "end_turn",
        }

        self.assertEqual(_detect_intent_tool_calls("/v1/messages", response, body), [])

    def test_intent_detection_falls_back_to_declared_bash_when_ls_absent(self):
        from src.gateway_tool_runtime import _detect_intent_tool_calls

        body = {
            "model": "weak",
            "messages": [{"role": "user", "content": "列出当前目录"}],
            "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        }
        response = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ls ."}],
            "stop_reason": "end_turn",
        }

        calls = _detect_intent_tool_calls("/v1/messages", response, body)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "Bash")
        self.assertEqual(calls[0].arguments.get("command"), "ls .")

    def test_intent_detection_gather_project_followup_uses_declared_bash(self):
        from src.gateway_tool_runtime import _detect_intent_tool_calls

        body = {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "分析这套项目"},
                {"role": "assistant", "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "codebase-onboarding"}}]},
                {"role": "user", "content": [{"type": "tool_result", "content": "Successfully loaded skill", "tool_use_id": "x"}]},
            ],
            "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        }
        response = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Let me gather the project structure and key files to analyze this project."}],
            "stop_reason": "end_turn",
        }

        calls = _detect_intent_tool_calls("/v1/messages", response, body)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "Bash")
        self.assertIn("find", calls[0].arguments.get("command", ""))

    def test_chat_only_read_uses_declared_read_file_path_schema(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [{"role": "user", "content": "读取 README.md"}],
            "tools": [{
                "name": "Read",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            }],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "Read")
        self.assertTrue(str(tool_use[0].get("input", {}).get("file_path", "")).endswith("/README.md"))

    def test_agent_planner_does_not_repeat_read_after_tool_result(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "读取 README.md"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "planner_read_file_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "Read", "input": {"file_path": "README.md"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "planner_read_file_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "content": "# TestProject\n\nThis is a test."},
                ]},
            ],
            "tools": [{
                "name": "Read",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            }],
            "max_tokens": 4096,
        })

        self.assertNotEqual(result.get("stop_reason"), "tool_use")
        self.assertFalse([b for b in (result.get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_use"])

    def test_agent_planner_does_not_repeat_responses_read_after_function_output(self):
        result = self._run("/v1/responses", {
            "model": "weak",
            "input": [
                {"role": "user", "content": "Read README.md"},
                {
                    "type": "function_call",
                    "call_id": "call_read_1",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "sed -n '1,240p' README.md"}),
                },
                {"type": "function_call_output", "call_id": "call_read_1", "output": "# TestProject\n\nThis is a test."},
            ],
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            }],
            "max_tokens": 4096,
        }, upstream_protocol="openai_responses")

        self.assertFalse([o for o in (result.get("output") or []) if isinstance(o, dict) and o.get("type") == "function_call"])
        self.assertEqual(result.get("status"), "completed")

    def test_chat_only_skill_uses_declared_claude_code_skill_schema(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [{"role": "user", "content": "Read skill live-skill"}],
            "tools": [{
                "name": "Skill",
                "input_schema": {
                    "type": "object",
                    "properties": {"skill": {"type": "string"}, "args": {"type": "string"}},
                    "required": ["skill"],
                    "additionalProperties": False,
                },
            }],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "Skill")
        self.assertEqual(tool_use[0].get("input"), {"skill": "live-skill"})

    def test_chat_only_web_search_uses_declared_downstream_tool_name(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        try:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)

            client = FakeClient([])
            result = run_tool_orchestration("/v1/chat/completions", {
                "model": "weak",
                "messages": [{"role": "user", "content": "请联网搜索 python pathlib latest docs"}],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }],
            }, client)

            self.assertEqual(client.requests, [])
            choice = (result.get("choices") or [{}])[0]
            call = ((choice.get("message") or {}).get("tool_calls") or [{}])[0]
            self.assertEqual(call.get("function", {}).get("name"), "web_search")
            self.assertIn("pathlib", call.get("function", {}).get("arguments", ""))
            self.assertEqual(choice.get("finish_reason"), "tool_calls")
            self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "web_search")
        finally:
            gateway.CONFIG_PATH = old_config

    def test_chat_only_declared_calculator_collision_surfaces_downstream_tool(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        try:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)

            client = FakeClient([])
            result = run_tool_orchestration("/v1/chat/completions", {
                "model": "weak",
                "messages": [{"role": "user", "content": "Calculate 6*7 for me"}],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "description": "Client-side calculator function",
                        "parameters": {
                            "type": "object",
                            "properties": {"expression": {"type": "string"}},
                            "required": ["expression"],
                        },
                    },
                }],
            }, client)

            self.assertEqual(client.requests, [])
            choice = (result.get("choices") or [{}])[0]
            call = ((choice.get("message") or {}).get("tool_calls") or [{}])[0]
            self.assertEqual(call.get("function", {}).get("name"), "calculator")
            self.assertIn("6*7", call.get("function", {}).get("arguments", ""))
            self.assertEqual(choice.get("finish_reason"), "tool_calls")
            self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "custom_function")
        finally:
            gateway.CONFIG_PATH = old_config

    def test_chat_only_custom_function_call_is_surfaced_without_upstream_native_support(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        try:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)

            client = FakeClient([])
            result = run_tool_orchestration("/v1/chat/completions", {
                "model": "weak",
                "messages": [{"role": "user", "content": "What's the weather in San Francisco today?"}],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a location",
                        "parameters": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                            "required": ["location"],
                        },
                    },
                }],
            }, client)

            self.assertEqual(client.requests, [])
            choice = (result.get("choices") or [{}])[0]
            call = ((choice.get("message") or {}).get("tool_calls") or [{}])[0]
            self.assertEqual(call.get("function", {}).get("name"), "get_weather")
            self.assertIn("San Francisco", call.get("function", {}).get("arguments", ""))
            self.assertEqual(choice.get("finish_reason"), "tool_calls")
            self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "custom_function")
        finally:
            gateway.CONFIG_PATH = old_config

    def test_chat_only_custom_function_call_infers_json_schema_arguments(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        try:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)

            client = FakeClient([])
            result = run_tool_orchestration("/v1/chat/completions", {
                "model": "weak",
                "messages": [{
                    "role": "user",
                    "content": (
                        "Please create_ticket with "
                        '{"title":"API tool bug","priority":"high","estimate":3,'
                        '"tags":["codex","tool"],"metadata":{"source":"claude-code"}}'
                    ),
                }],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "create_ticket",
                        "description": "Create a support ticket",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                                "estimate": {"type": "integer"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "metadata": {
                                    "type": "object",
                                    "properties": {"source": {"type": "string"}},
                                    "required": ["source"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["title", "priority", "estimate", "tags", "metadata"],
                            "additionalProperties": False,
                        },
                    },
                }],
            }, client)

            self.assertEqual(client.requests, [])
            choice = (result.get("choices") or [{}])[0]
            call = ((choice.get("message") or {}).get("tool_calls") or [{}])[0]
            self.assertEqual(call.get("function", {}).get("name"), "create_ticket")
            args = json.loads(call.get("function", {}).get("arguments") or "{}")
            self.assertEqual(args, {
                "title": "API tool bug",
                "priority": "high",
                "estimate": 3,
                "tags": ["codex", "tool"],
                "metadata": {"source": "claude-code"},
            })
            self.assertEqual(choice.get("finish_reason"), "tool_calls")
            self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("step"), "custom_function")
        finally:
            gateway.CONFIG_PATH = old_config

    def test_agent_planner_periodically_compacts_evidence_with_llm_summary(self):
        from unittest import mock
        from src.gateway_agent_planner import PlannerToolEvidence, _update_state_with_evidence

        evidence = [
            PlannerToolEvidence(f"call_{idx}", "Bash", f"tool output {idx} src/file_{idx}.py")
            for idx in range(4)
        ]
        with mock.patch("src.gateway_agent_planner._summarize_planner_evidence_via_llm", return_value="LLM COMPACT SUMMARY") as summarizer:
            state = _update_state_with_evidence({}, evidence)

        summarizer.assert_called_once()
        self.assertEqual(state.get("evidence_summary"), "LLM COMPACT SUMMARY")
        self.assertEqual(state.get("compaction_count"), 1)
        self.assertEqual(state.get("llm_compaction_count"), 1)

    def test_agent_runtime_events_record_dispatch_result_and_compaction(self):
        from src.gateway_agent_planner import (
            PlannerToolEvidence,
            _update_state_with_evidence,
            list_runtime_events,
            plan_downstream_tool_request,
            planner_state_snapshot,
        )

        td = self._setup_workspace()
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_every = os.environ.get("GATEWAY_AGENT_PLANNER_SUMMARY_EVERY")
        old_llm = os.environ.get("GATEWAY_AGENT_PLANNER_LLM_SUMMARY")
        try:
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            os.environ["GATEWAY_AGENT_PLANNER_SUMMARY_EVERY"] = "1"
            os.environ["GATEWAY_AGENT_PLANNER_LLM_SUMMARY"] = "0"
            import src.gateway_agent_planner as planner
            planner._STORE = None
            body = {
                "model": "weak",
                "metadata": {"session_id": "timeline-session", "user_id": json.dumps({"user_id": "timeline-user"})},
                "messages": [
                    {"role": "system", "content": "Available skills:\n- codebase-onboarding"},
                    {"role": "user", "content": "分析这套项目"},
                ],
                "tools": [{
                    "name": "Skill",
                    "input_schema": {"type": "object", "properties": {"skill": {"type": "string"}}, "required": ["skill"]},
                }],
            }
            decision = plan_downstream_tool_request("/v1/messages", body)
            self.assertIsNotNone(decision)
            self.assertTrue(decision.calls)
            snapshot = planner_state_snapshot(decision.state)
            self.assertEqual(snapshot["intent"]["kind"], "project_analysis")
            self.assertEqual(snapshot["intent"]["workflow"], "project_analysis")
            self.assertIn("declared_tools", snapshot["intent"]["signals"])
            self.assertEqual(snapshot["last_decision"]["workflow"], "project_analysis")
            self.assertEqual(snapshot["last_decision"]["step"], "codebase_onboarding")
            self.assertEqual(snapshot["last_decision"]["calls"][0]["name"], decision.calls[0].name)
            persisted = planner._store().list_recent(10, tenant_contains="timeline-user")
            self.assertEqual(persisted[0]["intent"]["kind"], "project_analysis")
            self.assertEqual(persisted[0]["last_decision"]["step"], "codebase_onboarding")
            state = _update_state_with_evidence(
                decision.state,
                [PlannerToolEvidence(decision.calls[0].call_id, decision.calls[0].name, "loaded onboarding", False)],
            )
            self.assertEqual(state.get("evidence_count"), 1)

            events = list_runtime_events(20, tenant_contains="timeline-user")
            event_types = {event["event_type"] for event in events}
            self.assertIn("intent_classification", event_types)
            self.assertIn("tool_dispatch", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("evidence_compaction", event_types)
            intent_event = next(event for event in events if event["event_type"] == "intent_classification")
            self.assertEqual(intent_event["metadata"]["intent"]["kind"], "project_analysis")
            dispatch = next(event for event in events if event["event_type"] == "tool_dispatch")
            self.assertEqual(dispatch["workflow"], "project_analysis")
            self.assertEqual(dispatch["step"], "codebase_onboarding")
            self.assertEqual(dispatch["metadata"]["calls"][0]["name"], decision.calls[0].name)
        finally:
            if old_runtime is None:
                os.environ.pop("GATEWAY_RUNTIME_DIR", None)
            else:
                os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws
            if old_every is None:
                os.environ.pop("GATEWAY_AGENT_PLANNER_SUMMARY_EVERY", None)
            else:
                os.environ["GATEWAY_AGENT_PLANNER_SUMMARY_EVERY"] = old_every
            if old_llm is None:
                os.environ.pop("GATEWAY_AGENT_PLANNER_LLM_SUMMARY", None)
            else:
                os.environ["GATEWAY_AGENT_PLANNER_LLM_SUMMARY"] = old_llm
            planner._STORE = None

    def test_agent_planner_records_completed_steps_from_planner_tool_ids(self):
        from src.gateway_agent_planner import PlannerToolEvidence, _update_state_with_evidence

        state = _update_state_with_evidence({}, [
            PlannerToolEvidence(
                "planner_project_structure_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "mcp__codebase_memory_mcp__get_architecture",
                "architecture evidence",
            )
        ])

        self.assertIn("project_structure", state.get("completed_steps") or [])

    def test_agent_planner_session_key_stays_stable_across_tool_result_turns(self):
        from src.gateway_agent_planner import planner_session_key

        td = self._setup_workspace()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        try:
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            first_turn = {
                "model": "weak",
                "messages": [{"role": "user", "content": "运行测试并修复"}],
            }
            tool_result_turn = {
                "model": "weak",
                "messages": [
                    {"role": "user", "content": "运行测试并修复"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "bash_1", "name": "Bash", "input": {"command": "python3 -m pytest -q"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "bash_1", "content": "exit_code=1\nFAILED tests/test_app.py"},
                    ]},
                ],
            }

            self.assertEqual(
                planner_session_key("/v1/messages", first_turn),
                planner_session_key("/v1/messages", tool_result_turn),
            )
        finally:
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws

    def test_agent_planner_session_key_ignores_recalled_memory_anchor_noise(self):
        from src.gateway_agent_planner import planner_session_key

        td = self._setup_workspace()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        try:
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            base = {
                "model": "weak",
                "messages": [{"role": "user", "content": "分析这套项目"}],
            }
            with_memory_a = {
                "model": "weak",
                "messages": [{"role": "user", "content": [
                    {
                        "type": "text",
                        "text": "[Gateway recalled memory]\n[Conversation Memories]\n- 上次读取 OLD_A.md\n\n",
                    },
                    {"type": "text", "text": "分析这套项目"},
                ]}],
            }
            with_memory_b = {
                "model": "weak",
                "messages": [{"role": "user", "content": [
                    {
                        "type": "text",
                        "text": "[Gateway recalled memory]\n[Conversation Memories]\n- 上次读取 OLD_B.md\n\n",
                    },
                    {"type": "text", "text": "分析这套项目"},
                ]}],
            }

            self.assertEqual(
                planner_session_key("/v1/messages", base),
                planner_session_key("/v1/messages", with_memory_a),
            )
            self.assertEqual(
                planner_session_key("/v1/messages", with_memory_a),
                planner_session_key("/v1/messages", with_memory_b),
            )
        finally:
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws

    def test_agent_planner_responses_session_key_ignores_recalled_memory_anchor_noise(self):
        from src.gateway_agent_planner import planner_session_key

        td = self._setup_workspace()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        try:
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            base = {"model": "weak", "input": "分析这套项目"}
            with_memory = {
                "model": "weak",
                "input": "[Gateway recalled memory]\n[Conversation Memories]\n- 上次读取 OLD.md\n\n分析这套项目",
            }

            self.assertEqual(
                planner_session_key("/v1/responses", base),
                planner_session_key("/v1/responses", with_memory),
            )
        finally:
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws

    def test_agent_planner_session_key_is_tenant_scoped_for_remote_service(self):
        from src.gateway_agent_planner import planner_session_key
        from src.gateway_tool_runtime import _workspace_scope

        td = pathlib.Path(self._setup_workspace())
        body_a = {
            "model": "weak",
            "metadata": {"session_id": "shared-session", "user_id": json.dumps({"user_id": "user-a"})},
            "messages": [{"role": "user", "content": "分析这套项目"}],
        }
        body_b = {
            "model": "weak",
            "metadata": {"session_id": "shared-session", "user_id": json.dumps({"user_id": "user-b"})},
            "messages": [{"role": "user", "content": "分析这套项目"}],
        }
        with _workspace_scope(td):
            key_a = planner_session_key("/v1/messages", body_a)
            key_b = planner_session_key("/v1/messages", body_b)
        self.assertNotEqual(key_a, key_b)
        self.assertIn("tenant:user-a", key_a)
        self.assertIn("tenant:user-b", key_b)

    def test_agent_planner_session_key_without_scope_does_not_use_gateway_env_root_for_remote_identity(self):
        from src.gateway_agent_planner import planner_session_key

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            service_root = pathlib.Path(td) / "gateway-service-root"
            service_root.mkdir()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                gateway.save_config(cfg)

                body = {
                    "model": "weak",
                    "metadata": {
                        "session_id": "remote-planner-session",
                        "user_id": json.dumps({"user_id": "remote-planner-user"}),
                    },
                    "messages": [{"role": "user", "content": "分析这套项目"}],
                }
                key_a = planner_session_key("/v1/messages", body)
                key_b = planner_session_key("/v1/messages", body)

                self.assertEqual(key_a, key_b)
                self.assertIn("tenant:remote-planner-user", key_a)
                self.assertIn("anonymous_spaces", key_a)
                self.assertNotIn(str(service_root.resolve()), key_a)
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_agent_planner_session_key_accepts_metadata_tenant_alias(self):
        from src.gateway_agent_planner import planner_session_key
        from src.gateway_tool_runtime import _workspace_scope

        td = pathlib.Path(self._setup_workspace())
        body = {
            "model": "weak",
            "metadata": {"tenant": "tenant-alias-user", "session_id": "shared-session"},
            "messages": [{"role": "user", "content": "分析这套项目"}],
        }
        with _workspace_scope(td):
            key = planner_session_key("/v1/messages", body)
        self.assertIn("tenant:tenant-alias-user", key)
        self.assertIn("session_id:shared-session", key)

    def test_agent_planner_uses_history_only_for_followup_not_plain_thanks(self):
        from src.gateway_agent_planner import classify_planner_intent

        body = {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "分析这套项目"},
                {"role": "assistant", "content": "我先读取项目结构。"},
                {"role": "user", "content": "谢谢"},
            ],
            "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        }

        intent = classify_planner_intent("/v1/messages", body)
        self.assertEqual(intent["kind"], "plain_chat")
        self.assertEqual(intent["workflow"], "chat_only_synthesis")

    def test_agent_planner_plain_thanks_after_project_history_does_not_dispatch_project_tool(self):
        from src.gateway_agent_planner import plan_downstream_tool_request

        body = {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "分析这套项目"},
                {"role": "assistant", "content": "我先读取项目结构。"},
                {"role": "user", "content": "谢谢"},
            ],
            "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        }

        decision = plan_downstream_tool_request("/v1/messages", body)
        self.assertIsNone(decision)

    def test_agent_planner_uses_history_for_explicit_project_followup(self):
        from src.gateway_agent_planner import classify_planner_intent

        body = {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "分析这套项目"},
                {"role": "assistant", "content": "我先读取项目结构。"},
                {"role": "user", "content": "继续"},
            ],
            "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        }

        intent = classify_planner_intent("/v1/messages", body)
        self.assertEqual(intent["kind"], "project_analysis")
        self.assertEqual(intent["workflow"], "project_analysis")
        self.assertIn("conversation_followup", intent.get("signals") or [])

    def test_agent_planner_history_validation_does_not_pollute_plain_followup(self):
        from src.gateway_agent_planner import classify_planner_intent

        body = {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "运行测试"},
                {"role": "assistant", "content": "测试已运行。"},
                {"role": "user", "content": "ok"},
            ],
            "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        }

        intent = classify_planner_intent("/v1/messages", body)
        self.assertEqual(intent["kind"], "plain_chat")
        self.assertEqual(intent["workflow"], "chat_only_synthesis")

    def test_agent_planner_bounds_huge_plain_chat_before_intent_regexes(self):
        from src.gateway_agent_planner import classify_planner_intent

        huge = "hello, just remember this note. " + ("CURRENT-HUGE-FILLER-" * 2000)
        intent = classify_planner_intent(
            "/v1/responses",
            {"model": "weak", "input": huge, "metadata": {"session_id": "huge", "user_id": json.dumps({"user_id": "huge-user"})}},
        )
        self.assertEqual(intent["kind"], "plain_chat")
        self.assertEqual(intent["workflow"], "chat_only_synthesis")

    def test_agent_planner_ignores_recalled_memory_for_current_intent(self):
        from src.gateway_agent_planner import plan_downstream_tool_request

        td = self._setup_workspace()
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            body = {
                "model": "weak",
                "metadata": {"session_id": "memory-intent", "user_id": json.dumps({"user_id": "memory-user"})},
                "messages": [{"role": "user", "content": [
                    {
                        "type": "text",
                        "text": "[Gateway recalled memory]\n[Conversation Memories]\n- 上次用户要求读取 OLD.md 并分析项目\n\n",
                    },
                    {"type": "text", "text": "hi"},
                ]}],
                "tools": [{
                    "name": "Read",
                    "input_schema": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                    },
                }],
                "max_tokens": 4096,
            }

            self.assertIsNone(plan_downstream_tool_request("/v1/messages", body))
            persisted = planner._store().list_recent(10, tenant_contains="memory-user")
            self.assertTrue(persisted)
            self.assertEqual(persisted[0]["intent"]["kind"], "plain_chat")
            serialized_intent = json.dumps(persisted[0]["intent"], ensure_ascii=False)
            self.assertNotIn("OLD.md", serialized_intent)
        finally:
            if old_runtime is None:
                os.environ.pop("GATEWAY_RUNTIME_DIR", None)
            else:
                os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws
            if planner is not None:
                planner._STORE = None

    def test_agent_planner_prefers_current_request_over_recalled_memory_paths(self):
        from src.gateway_agent_planner import plan_downstream_tool_request

        td = self._setup_workspace()
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td
            body = {
                "model": "weak",
                "metadata": {"session_id": "memory-path", "user_id": json.dumps({"user_id": "memory-user"})},
                "messages": [{"role": "user", "content": [
                    {
                        "type": "text",
                        "text": "[Gateway recalled memory]\n[Conversation Memories]\n- 上次读取 OLD.md\n\n",
                    },
                    {"type": "text", "text": "请读取 README.md"},
                ]}],
                "tools": [{
                    "name": "Read",
                    "input_schema": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                    },
                }],
                "max_tokens": 4096,
            }

            decision = plan_downstream_tool_request("/v1/messages", body)
            self.assertIsNotNone(decision)
            self.assertEqual(decision.calls[0].name, "Read")
            serialized_args = json.dumps(decision.calls[0].arguments, ensure_ascii=False)
            self.assertIn("README.md", serialized_args)
            self.assertNotIn("OLD.md", serialized_args)
        finally:
            if old_runtime is None:
                os.environ.pop("GATEWAY_RUNTIME_DIR", None)
            else:
                os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws
            if planner is not None:
                planner._STORE = None

    def test_agent_planner_parallel_users_keep_intent_and_workspace_isolated(self):
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        planner = None
        try:
            import src.gateway_agent_planner as planner
            planner._STORE = None
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_RUNTIME_DIR"] = str(pathlib.Path(td) / "runtime")
            os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            cfg = gateway._default_config()
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            gateway.save_config(cfg)

            workspace_a = pathlib.Path(td) / "client-a"
            workspace_b = pathlib.Path(td) / "client-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            workspace_a_key = str(workspace_a.resolve())
            workspace_b_key = str(workspace_b.resolve())
            (workspace_a / "README.md").write_text("A\n", encoding="utf-8")
            (workspace_b / "README.md").write_text("B\n", encoding="utf-8")
            barrier = threading.Barrier(2)
            results: dict[str, Json] = {}
            errors: list[str] = []

            def worker(name: str, root: pathlib.Path) -> None:
                try:
                    barrier.wait(timeout=5)
                    client = FakeClient([])
                    body = {
                        "model": "weak",
                        "workspace_root": str(root),
                        "metadata": {"session_id": "parallel-session", "user_id": json.dumps({"user_id": name})},
                        "messages": [{"role": "user", "content": "请读取 README.md"}],
                        "tools": [{
                            "name": "Read",
                            "input_schema": {
                                "type": "object",
                                "properties": {"file_path": {"type": "string"}},
                                "required": ["file_path"],
                            },
                        }],
                        "max_tokens": 128,
                    }
                    results[name] = run_tool_orchestration("/v1/messages", body, client)
                    self.assertEqual(client.requests, [])
                except Exception as exc:  # pragma: no cover - assertion surface for worker threads
                    errors.append(f"{name}: {exc}")

            t1 = threading.Thread(target=worker, args=("parallel-user-a", workspace_a))
            t2 = threading.Thread(target=worker, args=("parallel-user-b", workspace_b))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(set(results), {"parallel-user-a", "parallel-user-b"})

            serialized_a = json.dumps(results["parallel-user-a"], ensure_ascii=False)
            serialized_b = json.dumps(results["parallel-user-b"], ensure_ascii=False)
            self.assertIn(str(workspace_a / "README.md"), serialized_a)
            self.assertIn(str(workspace_b / "README.md"), serialized_b)
            self.assertNotIn(str(workspace_b / "README.md"), serialized_a)
            self.assertNotIn(str(workspace_a / "README.md"), serialized_b)
            response_intent_a = (((results["parallel-user-a"].get("gateway_context") or {}).get("agent_planner") or {}).get("intent") or {})
            response_intent_b = (((results["parallel-user-b"].get("gateway_context") or {}).get("agent_planner") or {}).get("intent") or {})
            self.assertEqual(response_intent_a.get("kind"), "read_file")
            self.assertEqual(response_intent_a.get("workflow"), "generic_tool")
            self.assertEqual(response_intent_b.get("kind"), "read_file")
            self.assertEqual(response_intent_b.get("workflow"), "generic_tool")

            sessions_a = planner._store().list_recent(10, tenant_contains="parallel-user-a", workspace_contains=workspace_a_key)
            sessions_b = planner._store().list_recent(10, tenant_contains="parallel-user-b", workspace_contains=workspace_b_key)
            self.assertEqual(len(sessions_a), 1)
            self.assertEqual(len(sessions_b), 1)
            self.assertEqual(sessions_a[0]["intent"]["kind"], "read_file")
            self.assertEqual(sessions_b[0]["intent"]["kind"], "read_file")
            self.assertEqual(sessions_a[0]["workspace_key"], workspace_a_key)
            self.assertEqual(sessions_b[0]["workspace_key"], workspace_b_key)
        finally:
            gateway.CONFIG_PATH = old_config
            if old_runtime is None:
                os.environ.pop("GATEWAY_RUNTIME_DIR", None)
            else:
                os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws
            if planner is not None:
                planner._STORE = None

    def test_agent_planner_evidence_survives_upstream_context_compaction(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        try:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            cfg.setdefault("context", {})
            cfg["context"]["enabled"] = True
            cfg["context"]["fanout_enabled"] = False
            cfg["context"]["max_input_tokens"] = 80
            cfg["context"]["summary_max_chars"] = 500
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            weak_resp = {"id": "c1", "choices": [{"message": {"role": "assistant", "content": "final compact synthesis"},
                                                   "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            client = FakeClient([weak_resp])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "messages": [
                    {"role": "system", "content": "large harness\n" + ("SYSTEM-CONTEXT " * 1000)},
                    {"role": "user", "content": "分析这套项目\n" + ("user context " * 1000)},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "bash_1", "name": "Bash", "input": {"command": "find ."}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "bash_1", "content": "--- files ---\nREADME.md\nsrc/huge.py\n"},
                    ]},
                ],
                "tools": [{
                    "name": "Bash",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            }, client)

            self.assertEqual(result.get("content", [{}])[0].get("text"), "final compact synthesis")
            self.assertEqual(len(client.requests), 1)
            upstream_body = client.requests[0][1]
            full_prompt = json.dumps(upstream_body, ensure_ascii=False)
            self.assertIn("Gateway Agent Planner evidence", full_prompt)
            self.assertIn("src/huge.py", full_prompt)
            self.assertNotIn("gateway_context", upstream_body)
            self.assertNotIn("gateway_agent_planner", upstream_body)
            self.assertTrue((result.get("gateway_context") or {}).get("compacted"))
        finally:
            gateway.CONFIG_PATH = old_config
            os.chdir(old_cwd)
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws

    def test_agent_planner_code_search_infers_mcp_project_argument(self):
        old_project = os.environ.get("GATEWAY_CODEBASE_MEMORY_PROJECT")
        try:
            os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = "Users-sanbo-Desktop-ai_tool_functioncall"
            result = self._run("/v1/messages", {
                "model": "weak",
                "messages": [{"role": "user", "content": "搜索代码 gateway_tool_runtime"}],
                "tools": [{
                    "name": "mcp__codebase_memory_mcp__search_graph",
                    "input_schema": {
                        "type": "object",
                        "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                        "required": ["project", "query"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            })
        finally:
            if old_project is None:
                os.environ.pop("GATEWAY_CODEBASE_MEMORY_PROJECT", None)
            else:
                os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = old_project

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "mcp__codebase_memory_mcp__search_graph")
        self.assertEqual(tool_use[0].get("input", {}).get("project"), "Users-sanbo-Desktop-ai_tool_functioncall")
        self.assertIn("gateway_tool_runtime", tool_use[0].get("input", {}).get("query", ""))
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("workflow"), "code_search")

    def test_agent_planner_code_search_without_scope_does_not_infer_gateway_service_project(self):
        from src.gateway_agent_planner import plan_downstream_tool_request

        with tempfile.TemporaryDirectory() as td:
            old_config = gateway.CONFIG_PATH
            old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
            old_gateway_project = os.environ.get("GATEWAY_CODEBASE_MEMORY_PROJECT")
            old_project = os.environ.get("CODEBASE_MEMORY_PROJECT")
            service_root = pathlib.Path(td) / "gateway-service-root"
            service_root.mkdir()
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_root)
            os.environ.pop("GATEWAY_CODEBASE_MEMORY_PROJECT", None)
            os.environ.pop("CODEBASE_MEMORY_PROJECT", None)
            try:
                cfg = gateway._default_config()
                cfg["gateway"]["workspace_root"] = str(service_root)
                gateway.save_config(cfg)

                body = {
                    "model": "weak",
                    "metadata": {
                        "session_id": f"remote-code-search-{uuid.uuid4().hex}",
                        "user_id": json.dumps({"user_id": "remote-code-search-user"}),
                    },
                    "messages": [{"role": "user", "content": "搜索代码 gateway_tool_runtime"}],
                    "tools": [{
                        "name": "mcp__codebase_memory_mcp__search_graph",
                        "input_schema": {
                            "type": "object",
                            "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                            "required": ["project", "query"],
                            "additionalProperties": False,
                        },
                    }],
                }
                decision = plan_downstream_tool_request("/v1/messages", body)
                self.assertIsNotNone(decision)
                project = decision.calls[0].arguments.get("project")
                self.assertEqual(project, "default")
                self.assertNotIn("gateway-service-root", json.dumps(decision.calls[0].arguments))
                self.assertNotIn(str(service_root.resolve()), json.dumps(decision.calls[0].arguments))
            finally:
                gateway.CONFIG_PATH = old_config
                if old_root is None:
                    os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
                else:
                    os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
                if old_gateway_project is None:
                    os.environ.pop("GATEWAY_CODEBASE_MEMORY_PROJECT", None)
                else:
                    os.environ["GATEWAY_CODEBASE_MEMORY_PROJECT"] = old_gateway_project
                if old_project is None:
                    os.environ.pop("CODEBASE_MEMORY_PROJECT", None)
                else:
                    os.environ["CODEBASE_MEMORY_PROJECT"] = old_project

    def test_agent_planner_run_tests_uses_declared_shell_tool(self):
        result = self._run("/v1/responses", {
            "model": "weak",
            "input": "运行测试",
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            }],
        })

        calls = [o for o in (result.get("output") or []) if o.get("type") == "function_call"]
        self.assertEqual(calls[0].get("name"), "exec_command")
        args = json.loads(calls[0].get("arguments") or "{}")
        self.assertIn("pytest", args.get("cmd", ""))
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("workflow"), "test_build")

    def test_agent_planner_explicit_edit_uses_declared_edit_tool(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [{"role": "user", "content": "把 README.md 中的 `TestProject` 改成 `BetterProject`"}],
            "tools": [{
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
            }],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "Edit")
        self.assertTrue(tool_use[0].get("input", {}).get("file_path", "").endswith("/README.md"))
        self.assertEqual(tool_use[0].get("input", {}).get("old_string"), "TestProject")
        self.assertEqual(tool_use[0].get("input", {}).get("new_string"), "BetterProject")
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("workflow"), "edit")

    def test_agent_planner_reads_failure_file_after_test_result(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "运行测试"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "bash_test", "name": "Bash", "input": {"command": "python3 -m pytest -q"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "bash_test", "content": "FAILED tests/test_app.py::test_x\nTraceback\n  File \"src/app.py\", line 12, in run\nAssertionError\nexit_code=1"},
                ]},
            ],
            "tools": [{
                "name": "Read",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            }],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "Read")
        self.assertTrue(tool_use[0].get("input", {}).get("file_path", "").endswith("/src/app.py"))
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("workflow"), "fix_loop")

    def test_fix_loop_reads_source_followup_import_after_diagnostic_read(self):
        result = self._run("/v1/messages", {
            "model": "weak",
            "messages": [
                {"role": "user", "content": "运行测试并修复"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "bash_test", "name": "Bash", "input": {"command": "python3 -m pytest -q"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "bash_test", "content": "FAILED tests/test_app.py::test_x\nTraceback\n  File \"src/app.py\", line 12, in run\nAssertionError\nexit_code=1"},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "planner_diagnostic_read_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "Read", "input": {"file_path": "src/app.py"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "planner_diagnostic_read_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "content": "from src.helper import check\n\ndef run():\n    return check()"},
                ]},
            ],
            "tools": [{
                "name": "Read",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            }],
            "max_tokens": 4096,
        })

        tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
        self.assertEqual(tool_use[0].get("name"), "Read")
        self.assertTrue(tool_use[0].get("input", {}).get("file_path", "").endswith("/src/helper.py"))
        planner_ctx = (result.get("gateway_context") or {}).get("agent_planner", {})
        self.assertEqual(planner_ctx.get("workflow"), "fix_loop")
        self.assertEqual(planner_ctx.get("step"), "source_followup_read")

    def test_fix_loop_upstream_patch_json_is_not_granted_tool_authority(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        try:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            patch_text = """```json
{"name":"Edit","arguments":{"file_path":"src/app.py","old_string":"return False","new_string":"return True"}}
```"""
            client = FakeClient([{"id": "c1", "choices": [{"message": {"role": "assistant", "content": patch_text}, "finish_reason": "stop"}]}])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "messages": [
                    {"role": "user", "content": "运行测试并修复"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "bash_test", "name": "Bash", "input": {"command": "python3 -m pytest -q"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "bash_test", "content": "FAILED tests/test_app.py::test_x\n  File \"src/app.py\", line 12\nexit_code=1"},
                    ]},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "read_1", "name": "Read", "input": {"file_path": "src/app.py"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "read_1", "content": "def ok():\n    return False\n"},
                    ]},
                ],
                "tools": [{
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
                }],
                "max_tokens": 4096,
            }, client)

            self.assertEqual(len(client.requests), 1)
            upstream_messages = client.requests[0][1].get("messages") or []
            self.assertIn("Gateway Agent Planner evidence", upstream_messages[0].get("content", ""))
            self.assertNotIn("tools", client.requests[0][1])
            tool_use = [b for b in (result.get("content") or []) if b.get("type") == "tool_use"]
            self.assertEqual(tool_use, [])
            text = "\n".join(str(b.get("text") or "") for b in (result.get("content") or []) if b.get("type") == "text")
            self.assertIn('"name":"Edit"', text)
            self.assertTrue((result.get("gateway_context") or {}).get("chat_only_synthesis"))
        finally:
            gateway.CONFIG_PATH = old_config
            os.chdir(old_cwd)
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws

    def test_qa_loop_reruns_tests_after_edit_result(self):
        result = self._run("/v1/responses", {
            "model": "weak",
            "input": [
                {"role": "user", "content": "运行测试并修复"},
                {"role": "assistant", "tool_calls": [
                    {"id": "bash_test", "type": "function", "function": {"name": "exec_command", "arguments": "{\"cmd\":\"python3 -m pytest -q\"}"}},
                    {"id": "edit_1", "type": "function", "function": {"name": "Edit", "arguments": "{\"file_path\":\"src/app.py\",\"old_string\":\"return False\",\"new_string\":\"return True\"}"}},
                ]},
                {"type": "function_call_output", "call_id": "edit_1", "output": "edited src/app.py; replacements=1"},
            ],
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            }],
        })

        calls = [o for o in (result.get("output") or []) if o.get("type") == "function_call"]
        self.assertEqual(calls[0].get("name"), "exec_command")
        args = json.loads(calls[0].get("arguments") or "{}")
        self.assertIn("pytest", args.get("cmd", ""))
        self.assertEqual((result.get("gateway_context") or {}).get("agent_planner", {}).get("workflow"), "qa_loop")

    def test_qa_loop_passes_to_final_synthesis_after_validation_success(self):
        from src.gateway_config import save_config
        from src.gateway_tool_runtime import run_tool_orchestration

        td = self._setup_workspace()
        old_config = gateway.CONFIG_PATH
        old_cwd = os.getcwd()
        old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        try:
            gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
            cfg = gateway._default_config()
            cfg["gateway"]["workspace_root"] = td
            cfg["gateway"]["tool_mode"] = "orchestrate"
            cfg["upstream"]["tools_enabled"] = "adapter"
            cfg["upstream"]["protocol"] = "openai_chat"
            cfg["upstream"]["capabilities"]["supports_tools"] = False
            cfg["upstream"]["capabilities"]["supports_function_calls"] = False
            save_config(cfg)
            os.chdir(td)
            os.environ["GATEWAY_WORKSPACE_ROOT"] = td

            client = FakeClient([{"id": "c1", "choices": [{"message": {"role": "assistant", "content": "修复完成，测试已通过。"}, "finish_reason": "stop"}]}])
            result = run_tool_orchestration("/v1/messages", {
                "model": "weak",
                "messages": [
                    {"role": "user", "content": "运行测试并修复"},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "edit_1", "name": "Edit", "input": {"file_path": "src/app.py", "old_string": "return False", "new_string": "return True"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "edit_1", "content": "edited src/app.py; replacements=1"},
                    ]},
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "bash_validate", "name": "Bash", "input": {"command": "python3 -m pytest -q"}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "bash_validate", "content": "1 passed in 0.01s\nexit_code=0"},
                    ]},
                ],
                "tools": [{
                    "name": "Bash",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                }],
                "max_tokens": 4096,
            }, client)

            self.assertEqual(result.get("stop_reason"), "end_turn")
            self.assertEqual(result.get("content", [{}])[0].get("text"), "修复完成，测试已通过。")
            self.assertEqual(len(client.requests), 1)
            system_text = (client.requests[0][1].get("messages") or [{}])[0].get("content", "")
            self.assertIn("Gateway Agent Planner evidence", system_text)
            self.assertIn("exit_code=0", system_text)
        finally:
            gateway.CONFIG_PATH = old_config
            os.chdir(old_cwd)
            if old_ws is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws

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
        self.assertEqual(tools_enabled, "adapter")
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

    def test_text_tool_adapter_injects_for_chinese_project_analysis_intent(self):
        from src.gateway_streaming import _merge_builtin_tools

        body = {
            "model": "test-model",
            "gateway_context": {"client_can_handle_implicit_tools": True},
            "messages": [
                {"role": "user", "content": "分析这套项目"},
            ],
            "max_tokens": 100,
        }

        result = _merge_builtin_tools("/v1/chat/completions", body)

        self.assertNotIn("tools", result)
        system_content = str(result["messages"][0].get("content", ""))
        self.assertIn("Tool Call Gateway adapter", system_content)
        user_content = str(result["messages"][-1].get("content", ""))
        self.assertIn("[IMPORTANT: Tool Call Gateway adapter is active", user_content)

    def test_text_tool_adapter_keeps_plain_chat_plain_without_tool_intent(self):
        from src.gateway_streaming import _merge_builtin_tools

        cases = [
            "Tell me a story about my cat.",
            "My cat is sick.",
            "The head office is closed.",
            "Help me find a way to stay focused.",
            "如何分析一个项目的商业价值？",
            "分析这套项目",
        ]
        for content in cases:
            body = {
                "model": "test-model",
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 100,
            }

            result = _merge_builtin_tools("/v1/chat/completions", body)

            self.assertEqual(result["messages"], body["messages"])
            self.assertNotIn("tools", result)
            self.assertNotIn("tool_choice", result)

    def test_text_tool_adapter_keeps_workspace_metadata_plain_without_capability(self):
        from src.gateway_streaming import _merge_builtin_tools

        cases = [
            {"project_dir": "relative/project"},
            {"project_dir": self._td.name},
            {"cwd": "/"},
        ]
        for metadata in cases:
            body = {
                "model": "test-model",
                "metadata": metadata,
                "messages": [{"role": "user", "content": "分析这套项目"}],
                "max_tokens": 100,
            }

            result = _merge_builtin_tools("/v1/chat/completions", body)

            self.assertEqual(result["messages"], body["messages"])
            self.assertNotIn("tools", result)
            self.assertNotIn("tool_choice", result)

    def test_text_tool_adapter_keeps_message_embedded_cwd_plain(self):
        from src.gateway_streaming import _merge_builtin_tools

        body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "CWD: /tmp\n分析这套项目"}],
            "max_tokens": 100,
        }

        result = _merge_builtin_tools("/v1/chat/completions", body)

        self.assertEqual(result["messages"], body["messages"])
        self.assertNotIn("tools", result)
        self.assertNotIn("tool_choice", result)

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
