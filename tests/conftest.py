"""Test configuration and fixtures for gateway tests."""
from __future__ import annotations

import os
import pathlib
import sys
import json
import hashlib
import tempfile
import threading
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest

# Add src to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True)
def _reset_caches():
    """Reset all caches before each test to prevent cross-test contamination."""
    try:
        from src.gateway_cache import reset_caches
        reset_caches()
    except Exception:
        pass
    yield
    try:
        from src.gateway_cache import reset_caches
        reset_caches()
    except Exception:
        pass


def _hash_key(key: str) -> str:
    """Hash API key for safe storage."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@pytest.fixture
def mock_upstream_config() -> dict[str, Any]:
    """Mock upstream configuration for testing."""
    return {
        "base_url": os.environ.get("TEST_UPSTREAM_URL", "http://localhost:9999"),
        "api_key": "",  # Never store real keys in test code
        "model": "test-model",
        "protocol": "openai_chat",
        "tools_enabled": "adapter",
        "timeout_seconds": 30,
        "max_input_tokens": 128000,
        "max_output_tokens": 4096,
        "capabilities": {
            "supports_streaming": True,
            "supports_tools": False,
            "supports_function_calls": False,
        },
    }


@pytest.fixture
def mock_gateway_config(mock_upstream_config: dict) -> dict[str, Any]:
    """Mock gateway configuration for testing."""
    return {
        "admin": {
            "username": "admin",
            "password_hash": _hash_key("test-admin-password"),
            "must_change_password": False,
        },
        "upstream": mock_upstream_config,
        "gateway": {
            "tool_mode": "orchestrate",
            "max_tool_rounds": 5,
            "workspace_root": tempfile.mkdtemp(),
            "allow_write_tools": True,
            "allow_shell_tools": False,
            "max_concurrent_requests": 32,
            "text_tool_call_fallback_enabled": True,
        },
        "context": {
            "enabled": True,
            "max_input_tokens": 128000,
            "keep_recent_messages": 12,
            "summary_max_chars": 6000,
            "fanout_enabled": True,
            "fanout_chunk_tokens": 50000,
            "fanout_max_workers": 4,
            "memory_enabled": True,
            "memory_max_items": 100,
            "memory_recall_limit": 5,
            "memory_inject_max_chars": 3000,
        },
        "cache": {
            "enabled": True,
            "max_entries": 100,
            "similarity_threshold": 0.92,
            "ttl_seconds": 300,
        },
        "downstream_keys": [
            {
                "name": "test-key",
                "key_hash": _hash_key("test-api-key"),
                "prefix": "test-api",
                "enabled": True,
                "protocols": ["models", "chat_completions", "responses", "messages"],
            }
        ],
    }


@pytest.fixture
def temp_workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a temporary workspace for testing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print('hello')")
    (workspace / "README.md").write_text("# Test Project")
    return workspace


@pytest.fixture
def sample_chat_request() -> dict[str, Any]:
    """Sample OpenAI chat completion request."""
    return {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, how are you?"},
        ],
        "temperature": 0.7,
        "max_tokens": 100,
    }


@pytest.fixture
def sample_responses_request() -> dict[str, Any]:
    """Sample OpenAI responses request."""
    return {
        "model": "test-model",
        "input": [
            {"role": "user", "content": "What is the weather today?"},
        ],
        "instructions": "You are a weather assistant.",
    }


@pytest.fixture
def sample_messages_request() -> dict[str, Any]:
    """Sample Anthropic messages request."""
    return {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "Tell me a joke."},
        ],
        "max_tokens": 200,
        "system": "You are a comedian.",
    }


@pytest.fixture
def sample_tool_calls_response() -> dict[str, Any]:
    """Sample response with tool calls (OpenAI format)."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression": "2+2"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    }


@pytest.fixture
def sample_multi_tool_calls_response() -> dict[str, Any]:
    """Sample response with multiple tool calls."""
    return {
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
                                "name": "read_file",
                                "arguments": '{"path": "src/main.py"}',
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "list_directory",
                                "arguments": '{"path": "."}',
                            },
                        },
                        {
                            "id": "call_3",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression": "10*5"}',
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


@pytest.fixture
def mock_upstream_server():
    """Mock upstream HTTP server for testing."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json

    class MockHandler(BaseHTTPRequestHandler):
        responses = []
        requests = []
        lock = threading.Lock()

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            request_data = json.loads(body) if body else {}

            with self.lock:
                self.requests.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": request_data,
                })

            if self.responses:
                with self.lock:
                    response = self.responses.pop(0)
            else:
                response = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Default mock response",
                            }
                        }
                    ]
                }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def do_GET(self):
            if self.path == "/v1/models":
                response = {
                    "data": [
                        {"id": "test-model", "object": "model"},
                        {"id": "another-model", "object": "model"},
                    ]
                }
            else:
                response = {"status": "ok"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, format, *args):
            pass  # Suppress logs during testing

    server = HTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "server": server,
        "port": port,
        "handler": MockHandler,
        "base_url": f"http://127.0.0.1:{port}",
    }

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def sqlite_db(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a temporary SQLite database for testing."""
    db_path = tmp_path / "test_gateway.sqlite3"
    return db_path


@pytest.fixture
def sample_conversation_history() -> list[dict[str, Any]]:
    """Sample conversation history for context testing."""
    return [
        {"role": "user", "content": "What is Python?"},
        {"role": "assistant", "content": "Python is a high-level programming language known for its simplicity and readability."},
        {"role": "user", "content": "How do I install it?"},
        {"role": "assistant", "content": "You can download Python from python.org or use a package manager like brew (macOS) or apt (Linux)."},
        {"role": "user", "content": "What are the best practices for Python coding?"},
        {"role": "assistant", "content": "Key Python best practices include: 1) Follow PEP 8 style guide, 2) Use type hints, 3) Write docstrings, 4) Use virtual environments, 5) Write tests."},
    ]


@pytest.fixture
def large_conversation_history() -> list[dict[str, Any]]:
    """Large conversation history for context compression testing."""
    messages = []
    for i in range(50):
        messages.append({
            "role": "user",
            "content": f"Question {i}: Tell me about topic {i}. " * 10,
        })
        messages.append({
            "role": "assistant",
            "content": f"Answer {i}: Here is information about topic {i}. " * 20,
        })
    return messages


@pytest.fixture
def sample_memories() -> list[dict[str, Any]]:
    """Sample memories for testing memory system."""
    return [
        {
            "id": 1,
            "session_key": "test-session",
            "workspace_root": "/test/workspace",
            "kind": "summary",
            "summary": "User is building a Python web application with FastAPI",
            "keywords_json": '["python", "fastapi", "web", "api"]',
            "importance": 8,
            "created_at": "2026-05-27T00:00:00Z",
            "last_used_at": "2026-05-27T00:00:00Z",
        },
        {
            "id": 2,
            "session_key": "test-session",
            "workspace_root": "/test/workspace",
            "kind": "fact",
            "summary": "User prefers using pytest for testing",
            "keywords_json": '["pytest", "testing", "python"]',
            "importance": 6,
            "created_at": "2026-05-27T01:00:00Z",
            "last_used_at": "2026-05-27T01:00:00Z",
        },
        {
            "id": 3,
            "session_key": "test-session",
            "workspace_root": "/test/workspace",
            "kind": "decision",
            "summary": "Decided to use SQLite for data storage instead of PostgreSQL",
            "keywords_json": '["sqlite", "database", "storage"]',
            "importance": 7,
            "created_at": "2026-05-27T02:00:00Z",
            "last_used_at": "2026-05-27T02:00:00Z",
        },
    ]


class MockEmbeddingService:
    """Mock embedding service for testing semantic features."""

    def __init__(self, dimension: int = 384):
        self.dimension = dimension
        self.call_count = 0

    def embed(self, text: str) -> list[float]:
        """Generate deterministic mock embedding from text."""
        self.call_count += 1
        # Create a deterministic embedding based on text hash
        hash_bytes = hashlib.sha256(text.encode()).digest()
        # Repeat hash to fill dimension
        repeated = (hash_bytes * (self.dimension // len(hash_bytes) + 1))[:self.dimension]
        # Normalize to [-1, 1]
        return [(b - 128) / 128 for b in repeated]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        return [self.embed(text) for text in texts]


@pytest.fixture
def mock_embedding_service() -> MockEmbeddingService:
    """Mock embedding service fixture."""
    return MockEmbeddingService()


class FakeUpstreamClient:
    """Fake upstream client for testing."""

    def __init__(self, responses: list[dict] | None = None):
        self.responses = list(responses or [])
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    def forward(self, path: str, body: dict) -> dict:
        with self.lock:
            self.requests.append({"path": path, "body": body})
            if self.responses:
                return self.responses.pop(0)
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "Fake response"}}
                ]
            }


@pytest.fixture
def fake_upstream_client() -> FakeUpstreamClient:
    """Fake upstream client fixture."""
    return FakeUpstreamClient()


# Skip tests that need real upstream
def needs_upstream(func):
    """Decorator to skip tests that need a real upstream server."""
    return pytest.mark.skipif(
        not os.environ.get("TEST_UPSTREAM_URL"),
        reason="TEST_UPSTREAM_URL not set",
    )(func)


# Skip tests that need embedding service
def needs_embedding(func):
    """Decorator to skip tests that need an embedding service."""
    return pytest.mark.skipif(
        not os.environ.get("TEST_EMBEDDING_URL"),
        reason="TEST_EMBEDDING_URL not set",
    )(func)
