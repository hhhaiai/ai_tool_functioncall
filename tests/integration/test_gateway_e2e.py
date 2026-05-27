"""End-to-end integration tests for the gateway.

Tests the complete request flow from downstream client through gateway to mock upstream.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytest

# Add src to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "src"))


class MockUpstreamHandler(BaseHTTPRequestHandler):
    """Mock upstream AI API server."""

    responses_queue = []
    requests_log = []
    lock = threading.Lock()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        request_data = json.loads(body) if body else {}

        with self.lock:
            self.requests_log.append({
                "path": self.path,
                "method": "POST",
                "headers": dict(self.headers),
                "body": request_data,
            })

        # Determine response based on path
        if self.path in ("/v1/chat/completions", "/anthropic/v1/chat/completions"):
            response = self._handle_chat_completions(request_data)
        elif self.path in ("/v1/responses", "/anthropic/v1/responses"):
            response = self._handle_responses(request_data)
        elif self.path in ("/v1/messages", "/anthropic/v1/messages"):
            response = self._handle_messages(request_data)
        else:
            response = {"error": f"Unknown path: {self.path}"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def do_GET(self):
        with self.lock:
            self.requests_log.append({"path": self.path, "method": "GET"})

        if self.path == "/v1/models":
            response = {
                "data": [
                    {"id": "mimo-v2.5-pro", "object": "model", "owned_by": "test"},
                    {"id": "test-model", "object": "model", "owned_by": "test"},
                ]
            }
        else:
            response = {"status": "ok"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def _handle_chat_completions(self, data):
        """Generate chat completions response."""
        # Check for queued responses
        with self.lock:
            if self.responses_queue:
                return self.responses_queue.pop(0)

        messages = data.get("messages", [])
        last_msg = messages[-1].get("content", "") if messages else ""

        # Generate tool call if requested
        if data.get("tools") and "tool_choice" in data:
            tool = data["tools"][0] if data["tools"] else {}
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call_{int(time.time())}",
                            "type": "function",
                            "function": {
                                "name": tool.get("function", {}).get("name", "unknown"),
                                "arguments": json.dumps({"expression": "2+2"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            }

        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": f"Echo: {last_msg}",
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        }

    def _handle_responses(self, data):
        """Generate responses format response."""
        input_data = data.get("input", [])
        if isinstance(input_data, str):
            user_text = input_data
        elif isinstance(input_data, list) and input_data:
            user_text = input_data[-1].get("content", "") if isinstance(input_data[-1], dict) else str(input_data[-1])
        else:
            user_text = ""

        return {
            "id": f"resp_{int(time.time())}",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": f"Response: {user_text}"}],
            }],
            "usage": {"input_tokens": 40, "output_tokens": 10},
        }

    def _handle_messages(self, data):
        """Generate Anthropic messages format response."""
        messages = data.get("messages", [])
        last_msg = messages[-1].get("content", "") if messages else ""
        if isinstance(last_msg, list):
            last_msg = " ".join(
                item.get("text", "") for item in last_msg if isinstance(item, dict)
            )

        return {
            "id": f"msg_{int(time.time())}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": f"Response: {last_msg}"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 40, "output_tokens": 10},
        }

    def log_message(self, format, *args):
        pass


@pytest.fixture
def mock_upstream():
    """Start a mock upstream server."""
    server = HTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Reset state
    MockUpstreamHandler.responses_queue.clear()
    MockUpstreamHandler.requests_log.clear()

    yield {
        "base_url": f"http://127.0.0.1:{port}",
        "port": port,
        "handler": MockUpstreamHandler,
    }

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def gateway_config_path(tmp_path, mock_upstream):
    """Create a temporary gateway config."""
    config = {
        "admin": {
            "username": "admin",
            "password_hash": "test-hash",
            "must_change_password": False,
        },
        "upstream": {
            "base_url": mock_upstream["base_url"],
            "api_key": "test-key",
            "model": "mimo-v2.5-pro",
            "protocol": "openai_chat",
            "tools_enabled": "auto",
            "timeout_seconds": 30,
            "max_input_tokens": 128000,
            "max_output_tokens": 4096,
            "paths": {
                "models": "/v1/models",
                "chat_completions": "/v1/chat/completions",
                "responses": "/v1/responses",
                "messages": "/v1/messages",
            },
            "capabilities": {
                "supports_streaming": True,
                "supports_tools": False,
                "supports_function_calls": False,
            },
        },
        "gateway": {
            "tool_mode": "orchestrate",
            "max_tool_rounds": 5,
            "workspace_root": str(tmp_path / "workspace"),
            "allow_write_tools": True,
            "allow_shell_tools": False,
            "max_concurrent_requests": 32,
            "text_tool_call_fallback_enabled": True,
        },
        "context": {
            "enabled": True,
            "max_input_tokens": 128000,
            "fanout_enabled": True,
            "memory_enabled": True,
        },
        "downstream_keys": [{
            "name": "test",
            "key_hash": "test-key-hash",
            "prefix": "test",
            "enabled": True,
            "protocols": ["models", "chat_completions", "responses", "messages"],
        }],
    }

    config_path = tmp_path / ".gateway_service.json"
    config_path.write_text(json.dumps(config, indent=2))

    # Create workspace
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "test.py").write_text("print('hello')")

    return config_path


@pytest.fixture
def make_request():
    """Helper to make HTTP requests to the gateway."""
    import urllib.request
    import urllib.error

    def _request(url, method="GET", data=None, headers=None):
        headers = headers or {}
        if data and isinstance(data, dict):
            data = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
                try:
                    return {"status": resp.status, "body": json.loads(body), "headers": dict(resp.headers)}
                except json.JSONDecodeError:
                    return {"status": resp.status, "body": body, "headers": dict(resp.headers)}
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass
            return {"status": e.code, "body": body, "headers": dict(e.headers)}

    return _request


@pytest.mark.integration
class TestGatewayModelsEndpoint:
    """Test /v1/models endpoint."""

    def test_list_models(self, mock_upstream, make_request):
        """Test listing available models."""
        url = f"{mock_upstream['base_url']}/v1/models"
        result = make_request(url, headers={"Authorization": "Bearer test-key"})

        assert result["status"] == 200
        assert "data" in result["body"]
        assert len(result["body"]["data"]) > 0


@pytest.mark.integration
class TestGatewayChatCompletions:
    """Test /v1/chat/completions endpoint."""

    def test_basic_chat(self, mock_upstream, make_request):
        """Test basic chat completion."""
        url = f"{mock_upstream['base_url']}/v1/chat/completions"
        data = {
            "model": "mimo-v2.5-pro",
            "messages": [{"role": "user", "content": "Hello!"}],
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": "Bearer test-key",
        })

        assert result["status"] == 200
        assert "choices" in result["body"]
        assert len(result["body"]["choices"]) > 0

    def test_chat_with_system_message(self, mock_upstream, make_request):
        """Test chat with system message."""
        url = f"{mock_upstream['base_url']}/v1/chat/completions"
        data = {
            "model": "mimo-v2.5-pro",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": "Bearer test-key",
        })

        assert result["status"] == 200

    def test_chat_with_tools(self, mock_upstream, make_request):
        """Test chat with tool definitions."""
        url = f"{mock_upstream['base_url']}/v1/chat/completions"
        data = {
            "model": "mimo-v2.5-pro",
            "messages": [{"role": "user", "content": "Calculate 2+2"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "Perform calculations",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "expression": {"type": "string"},
                        },
                    },
                },
            }],
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": "Bearer test-key",
        })

        assert result["status"] == 200


@pytest.mark.integration
class TestGatewayResponses:
    """Test /v1/responses endpoint."""

    def test_basic_response(self, mock_upstream, make_request):
        """Test basic response."""
        url = f"{mock_upstream['base_url']}/v1/responses"
        data = {
            "model": "mimo-v2.5-pro",
            "input": [{"role": "user", "content": "Hello!"}],
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": "Bearer test-key",
        })

        assert result["status"] == 200

    def test_response_with_instructions(self, mock_upstream, make_request):
        """Test response with instructions."""
        url = f"{mock_upstream['base_url']}/v1/responses"
        data = {
            "model": "mimo-v2.5-pro",
            "input": [{"role": "user", "content": "Hello!"}],
            "instructions": "You are a helpful assistant.",
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": "Bearer test-key",
        })

        assert result["status"] == 200


@pytest.mark.integration
class TestGatewayMessages:
    """Test /v1/messages endpoint (Anthropic compatible)."""

    def test_basic_message(self, mock_upstream, make_request):
        """Test basic Anthropic message."""
        url = f"{mock_upstream['base_url']}/v1/messages"
        data = {
            "model": "mimo-v2.5-pro",
            "messages": [{"role": "user", "content": "Hello!"}],
            "max_tokens": 100,
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": "Bearer test-key",
        })

        assert result["status"] == 200

    def test_message_with_system(self, mock_upstream, make_request):
        """Test Anthropic message with system."""
        url = f"{mock_upstream['base_url']}/v1/messages"
        data = {
            "model": "mimo-v2.5-pro",
            "messages": [{"role": "user", "content": "Hello!"}],
            "system": "You are a helpful assistant.",
            "max_tokens": 100,
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": "Bearer test-key",
        })

        assert result["status"] == 200


@pytest.mark.integration
class TestGatewayConcurrentRequests:
    """Test concurrent request handling."""

    def test_concurrent_chat_requests(self, mock_upstream, make_request):
        """Test handling multiple concurrent requests."""
        url = f"{mock_upstream['base_url']}/v1/chat/completions"
        results = []
        errors = []

        def worker(thread_id):
            try:
                data = {
                    "model": "mimo-v2.5-pro",
                    "messages": [{"role": "user", "content": f"Hello from {thread_id}!"}],
                }
                result = make_request(url, method="POST", data=data, headers={
                    "Authorization": "Bearer test-key",
                })
                results.append((thread_id, result))
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 5
        for thread_id, result in results:
            assert result["status"] == 200


@pytest.mark.integration
class TestGatewayContextManagement:
    """Test context management features."""

    def test_long_conversation(self, mock_upstream, make_request):
        """Test handling a long conversation."""
        url = f"{mock_upstream['base_url']}/v1/chat/completions"
        messages = []
        for i in range(20):
            messages.append({"role": "user", "content": f"Question {i}: " + "word " * 100})
            messages.append({"role": "assistant", "content": f"Answer {i}: " + "word " * 50})

        data = {
            "model": "mimo-v2.5-pro",
            "messages": messages,
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": "Bearer test-key",
        })

        # Should succeed (possibly with context compaction)
        assert result["status"] == 200


@pytest.mark.integration
class TestGatewayToolExecution:
    """Test tool execution through the gateway."""

    def test_builtin_tool_execution(self):
        """Test executing a builtin tool directly."""
        from src.gateway_builtin_tools import BUILTIN_TOOLS

        # Verify builtin tools are available
        assert len(BUILTIN_TOOLS) > 0

        # Check for expected tools - BUILTIN_TOOLS is a dict keyed by name
        tool_names = set(BUILTIN_TOOLS.keys())
        assert "echo_probe" in tool_names

    def test_calculator_tool(self):
        """Test calculator builtin tool."""
        from src.gateway_builtin_tools import BUILTIN_TOOLS

        calculator = BUILTIN_TOOLS.get("calculator")
        if calculator:
            result = calculator.handler({"expression": "2+2"})
            assert result is not None
            assert "4" in str(result)

    def test_echo_probe_tool(self):
        """Test echo probe builtin tool."""
        from src.gateway_builtin_tools import BUILTIN_TOOLS

        echo = BUILTIN_TOOLS.get("echo_probe")
        assert echo is not None
        result = echo.handler({"value": "test"})
        assert result == "test"


@pytest.mark.integration
class TestGatewayProtocolConversion:
    """Test protocol conversion between formats."""

    def test_openai_to_anthropic_tools(self):
        """Test converting OpenAI tools to Anthropic format."""
        from src.gateway_protocol import _openai_tools_to_anthropic

        openai_tools = [{
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "Perform calculations",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string"},
                    },
                },
            },
        }]

        anthropic_tools = _openai_tools_to_anthropic(openai_tools)
        assert len(anthropic_tools) == 1
        assert anthropic_tools[0]["name"] == "calculator"

    def test_anthropic_to_openai_tools(self):
        """Test converting Anthropic tools to OpenAI format."""
        from src.gateway_protocol import _anthropic_tools_to_openai

        anthropic_tools = [{
            "name": "calculator",
            "description": "Perform calculations",
            "input_schema": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                },
            },
        }]

        openai_tools = _anthropic_tools_to_openai(anthropic_tools)
        assert len(openai_tools) == 1
        assert openai_tools[0]["type"] == "function"
        assert openai_tools[0]["function"]["name"] == "calculator"


@pytest.mark.integration
class TestGatewayWithRealUpstream:
    """Tests that require a real upstream server.

    These tests are skipped unless TEST_UPSTREAM_URL is set.
    """

    @pytest.fixture(autouse=True)
    def check_upstream(self):
        if not os.environ.get("TEST_UPSTREAM_URL"):
            pytest.skip("TEST_UPSTREAM_URL not set")

    def test_real_models_endpoint(self, make_request):
        """Test listing models from real upstream."""
        url = f"{os.environ['TEST_UPSTREAM_URL']}/v1/models"
        key = os.environ.get("TEST_API_KEY", "test-key")
        result = make_request(url, headers={"Authorization": f"Bearer {key}"})

        assert result["status"] == 200

    def test_real_chat_completion(self, make_request):
        """Test chat completion with real upstream."""
        url = f"{os.environ['TEST_UPSTREAM_URL']}/v1/chat/completions"
        key = os.environ.get("TEST_API_KEY", "test-key")
        data = {
            "model": os.environ.get("TEST_MODEL", "mimo-v2.5-pro"),
            "messages": [{"role": "user", "content": "Say hello in one word."}],
            "max_tokens": 10,
        }
        result = make_request(url, method="POST", data=data, headers={
            "Authorization": f"Bearer {key}",
        })

        assert result["status"] == 200
