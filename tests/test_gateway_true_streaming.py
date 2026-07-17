from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading
import time
import urllib.request
import urllib.error
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from src import gateway_app as gateway
from src.gateway_errors import UpstreamHTTPError
from src.gateway_proxy import NativeProxyClient, UpstreamSSEEvent
from src.gateway_stream_state import UpstreamResponseAccumulator
from src.gateway_streaming import _DownstreamDeltaEmitter, _run_streaming_orchestration_scoped


def _event(payload: dict, event: str | None = None) -> UpstreamSSEEvent:
    return UpstreamSSEEvent(event, json.dumps(payload), len(json.dumps(payload).encode()))


class CaptureHandler:
    def __init__(self, on_write=None):
        self.parts = []
        callback = on_write

        class Writer:
            def write(inner_self, data):
                text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
                self.parts.append(text)
                if callback:
                    callback(text)

            def flush(inner_self):
                return

        self.wfile = Writer()

    def text(self) -> str:
        return "".join(self.parts)


def _native_config(root: pathlib.Path) -> dict:
    cfg = gateway._default_config()
    cfg["gateway"]["agent_planner_strict_every_turn"] = False
    cfg["gateway"]["local_planner_enabled"] = False
    cfg["gateway"]["tool_mode"] = "orchestrate"
    cfg["upstream"]["protocol"] = "openai_chat"
    cfg["upstream"]["tools_enabled"] = "on"
    cfg["upstream"]["capabilities"]["supports_tools"] = True
    cfg["upstream"]["capabilities"]["supports_function_calls"] = True
    cfg["context"]["enabled"] = False
    cfg["context"]["memory_enabled"] = False
    cfg["gateway"]["workspace_root"] = str(root)
    return cfg


def test_chat_accumulator_reconstructs_fragmented_text_and_tool_call():
    accumulator = UpstreamResponseAccumulator("/v1/chat/completions")
    deltas = accumulator.feed(None, json.dumps({
        "id": "chat-stream",
        "model": "m",
        "choices": [{
            "index": 0,
            "delta": {
                "content": "checking ",
                "tool_calls": [{
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "calculator", "arguments": "{\"expression\":"},
                }],
            },
            "finish_reason": None,
        }],
    }))
    assert [delta.text for delta in deltas] == ["checking "]
    accumulator.feed(None, json.dumps({
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"20+22\"}"}}]},
            "finish_reason": "tool_calls",
        }],
    }))

    response = accumulator.finalize()
    message = response["choices"][0]["message"]
    assert message["content"] == "checking "
    assert message["tool_calls"][0]["function"] == {
        "name": "calculator",
        "arguments": '{"expression":"20+22"}',
    }
    assert response["choices"][0]["finish_reason"] == "tool_calls"


def test_anthropic_accumulator_reconstructs_text_and_tool_input():
    accumulator = UpstreamResponseAccumulator("/v1/messages")
    accumulator.feed("message_start", json.dumps({
        "type": "message_start",
        "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 3}},
    }))
    accumulator.feed("content_block_start", json.dumps({
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }))
    assert accumulator.feed("content_block_delta", json.dumps({
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "hello"},
    }))[0].text == "hello"
    accumulator.feed("content_block_start", json.dumps({
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "tool_1", "name": "calculator", "input": {}},
    }))
    accumulator.feed("content_block_delta", json.dumps({
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"expression":"20+22"}'},
    }))
    accumulator.feed("message_delta", json.dumps({
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use"},
        "usage": {"output_tokens": 8},
    }))

    response = accumulator.finalize()
    assert response["content"][0] == {"type": "text", "text": "hello"}
    assert response["content"][1]["input"] == {"expression": "20+22"}
    assert response["stop_reason"] == "tool_use"


def test_responses_accumulator_reconstructs_text_delta():
    accumulator = UpstreamResponseAccumulator("/v1/responses")
    accumulator.feed("response.created", json.dumps({
        "type": "response.created",
        "response": {"id": "resp_1", "object": "response", "model": "m", "created_at": 1},
    }))
    accumulator.feed("response.output_item.added", json.dumps({
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"id": "msg_1", "type": "message", "status": "in_progress", "role": "assistant", "content": []},
    }))
    deltas = accumulator.feed("response.output_text.delta", json.dumps({
        "type": "response.output_text.delta",
        "item_id": "msg_1",
        "output_index": 0,
        "content_index": 0,
        "delta": "hello",
    }))
    assert deltas[0].text == "hello"
    response = accumulator.finalize()
    assert response["output"][0]["content"][0]["text"] == "hello"


def test_cross_protocol_streamed_tool_calls_survive_conversion():
    from src.gateway_protocol import _convert_response_to_downstream
    from src.gateway_tool_runtime import _extract_tool_calls

    anthropic = UpstreamResponseAccumulator("/v1/messages")
    anthropic.feed("message_start", json.dumps({
        "type": "message_start",
        "message": {"id": "msg_tool", "model": "m", "usage": {}},
    }))
    anthropic.feed("content_block_start", json.dumps({
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "id": "call_a", "name": "calculator", "input": {}},
    }))
    anthropic.feed("content_block_delta", json.dumps({
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"expression":"20+22"}'},
    }))
    anthropic.feed("message_delta", json.dumps({
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use"},
        "usage": {},
    }))
    chat_response = _convert_response_to_downstream(
        "/v1/chat/completions",
        anthropic.finalize(),
        "anthropic_messages",
    )
    chat_calls = _extract_tool_calls("/v1/chat/completions", chat_response)
    assert len(chat_calls) == 1
    assert chat_calls[0].name == "calculator"
    assert chat_calls[0].arguments == {"expression": "20+22"}

    responses = UpstreamResponseAccumulator("/v1/responses")
    responses.feed("response.created", json.dumps({
        "type": "response.created",
        "response": {"id": "resp_tool", "object": "response", "model": "m"},
    }))
    responses.feed("response.output_item.added", json.dumps({
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"id": "fc_1", "type": "function_call", "call_id": "call_r", "name": "calculator", "arguments": ""},
    }))
    responses.feed("response.function_call_arguments.delta", json.dumps({
        "type": "response.function_call_arguments.delta",
        "output_index": 0,
        "delta": '{"expression":"20+22"}',
    }))
    messages_response = _convert_response_to_downstream(
        "/v1/messages",
        responses.finalize(),
        "openai_responses",
    )
    message_calls = _extract_tool_calls("/v1/messages", messages_response)
    assert len(message_calls) == 1
    assert message_calls[0].name == "calculator"
    assert message_calls[0].arguments == {"expression": "20+22"}


def test_safe_plain_chat_delta_reaches_client_before_upstream_finishes():
    first_written = threading.Event()
    release_second = threading.Event()
    errors = []

    class StreamingClient:
        def __init__(self):
            self.requests = []

        def stream(self, path, body):
            self.requests.append((path, body))
            yield _event({
                "id": "chat_live",
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
            })
            assert release_second.wait(5)
            yield _event({
                "id": "chat_live",
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": None}],
            })
            yield _event({
                "id": "chat_live",
                "model": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })
            yield UpstreamSSEEvent(None, "[DONE]", 14)

    def on_write(text):
        if "hello" in text:
            first_written.set()

    with tempfile.TemporaryDirectory() as td:
        old_config = gateway.CONFIG_PATH
        gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
        try:
            cfg = _native_config(pathlib.Path(td))
            gateway.save_config(cfg)
            client = StreamingClient()
            handler = CaptureHandler(on_write)

            def run():
                try:
                    _run_streaming_orchestration_scoped(
                        handler,
                        "/v1/chat/completions",
                        {"model": "m", "stream": True, "messages": [{"role": "user", "content": "say hello"}]},
                        mode="orchestrate",
                        upstream_protocol="openai_chat",
                        gateway_cfg=cfg["gateway"],
                        max_rounds=3,
                        upstream=client,
                        context_cfg={"enabled": False},
                    )
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            assert first_written.wait(2), "first upstream delta was not forwarded before stream completion"
            assert thread.is_alive(), "request finished before the upstream released its second delta"
            release_second.set()
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert not errors
            text = handler.text()
            assert "hello" in text and " world" in text
            assert text.index("hello") < text.index(" world")
            assert '"finish_reason": "stop"' in text
            assert client.requests[0][1]["stream"] is False
        finally:
            gateway.CONFIG_PATH = old_config


def test_streaming_tool_call_fragments_execute_then_synthesize_final_answer():
    class StreamingToolClient:
        def __init__(self):
            self.requests = []

        def stream(self, path, body):
            self.requests.append((path, json.loads(json.dumps(body))))
            if len(self.requests) == 1:
                yield _event({
                    "id": "round_1",
                    "model": "m",
                    "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": [{
                            "index": 0,
                            "id": "call_calc",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": "{\"expression\":"},
                        }]},
                        "finish_reason": None,
                    }],
                })
                yield _event({
                    "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"20+22\"}"}}]},
                        "finish_reason": "tool_calls",
                    }],
                })
            else:
                yield _event({
                    "id": "round_2",
                    "model": "m",
                    "choices": [{"index": 0, "delta": {"content": "calculator returned 42"}, "finish_reason": None}],
                })
                yield _event({
                    "id": "round_2",
                    "model": "m",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                })
            yield UpstreamSSEEvent(None, "[DONE]", 14)

    with tempfile.TemporaryDirectory() as td:
        old_config = gateway.CONFIG_PATH
        gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
        try:
            cfg = _native_config(pathlib.Path(td))
            gateway.save_config(cfg)
            client = StreamingToolClient()
            handler = CaptureHandler()
            _run_streaming_orchestration_scoped(
                handler,
                "/v1/chat/completions",
                {
                    "model": "m",
                    "stream": True,
                    "gateway_context": {"client_can_handle_implicit_tools": True},
                    "messages": [{"role": "user", "content": "run a command to calculate 20+22"}],
                },
                mode="orchestrate",
                upstream_protocol="openai_chat",
                gateway_cfg=cfg["gateway"],
                max_rounds=3,
                upstream=client,
                context_cfg={"enabled": False},
            )

            assert len(client.requests) == 2
            tool_messages = [message for message in client.requests[1][1].get("messages", []) if message.get("role") == "tool"]
            assert tool_messages and "42" in tool_messages[0]["content"]
            text = handler.text()
            assert "tool_start" in text
            assert "tool_result" in text
            assert "calculator returned 42" in text
            assert "event: done" in text
        finally:
            gateway.CONFIG_PATH = old_config


def test_orchestration_falls_back_to_nonstreaming_when_upstream_declares_no_stream_support():
    class NonStreamingClient:
        supports_streaming = False

        def __init__(self):
            self.forward_calls = 0

        def stream(self, path, body):
            raise AssertionError("stream transport must not be used")

        def forward(self, path, body):
            self.forward_calls += 1
            return {
                "id": "fallback_nonstream",
                "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "fallback ok"}, "finish_reason": "stop"}],
            }

    with tempfile.TemporaryDirectory() as td:
        old_config = gateway.CONFIG_PATH
        gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
        try:
            cfg = _native_config(pathlib.Path(td))
            cfg["upstream"]["capabilities"]["supports_streaming"] = False
            gateway.save_config(cfg)
            client = NonStreamingClient()
            handler = CaptureHandler()
            _run_streaming_orchestration_scoped(
                handler,
                "/v1/chat/completions",
                {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hello"}]},
                mode="orchestrate",
                upstream_protocol="openai_chat",
                gateway_cfg=cfg["gateway"],
                max_rounds=2,
                upstream=client,
                context_cfg={"enabled": False},
            )
            assert client.forward_calls == 1
            assert "fallback ok" in handler.text()
        finally:
            gateway.CONFIG_PATH = old_config


def test_stream_generator_close_closes_upstream_response(monkeypatch):
    class Response:
        headers = {"content-type": "text/event-stream"}

        def __init__(self):
            self.closed = False
            self.lines = iter([
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n',
                b"\n",
                b'data: {"choices":[{"delta":{"content":"later"}}]}\n',
                b"\n",
            ])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.lines)

        def close(self):
            self.closed = True

    response = Response()
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="m")
    client._opener = SimpleNamespace(open=lambda *args, **kwargs: response)
    generator = client.stream("/v1/chat/completions", {"model": "m", "messages": []})
    first = next(generator)
    assert "hello" in first.data
    generator.close()
    assert response.closed is True


def test_stream_transport_rejects_single_oversized_sse_event():
    class Response:
        headers = {"content-type": "text/event-stream"}

        def __init__(self):
            self.closed = False
            self.lines = iter([b"data: " + (b"X" * 2000) + b"\n", b"\n"])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.lines)

        def close(self):
            self.closed = True

    response = Response()
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="m")
    client.max_stream_event_bytes = 1024
    client._opener = SimpleNamespace(open=lambda *args, **kwargs: response)

    with pytest.raises(UpstreamHTTPError) as exc_info:
        list(client.stream("/v1/chat/completions", {"model": "m", "messages": []}))
    assert exc_info.value.detail["type"] == "upstream_stream_event_too_large"
    assert response.closed is True


def test_stream_transport_retries_only_before_first_event(monkeypatch):
    class Response:
        headers = {"content-type": "text/event-stream"}

        def __init__(self, fail_midstream=False):
            self.closed = False
            self.fail_midstream = fail_midstream
            self.index = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.index += 1
            if self.index == 1:
                return b'data: {"choices":[{"delta":{"content":"hello"}}]}\n'
            if self.index == 2:
                return b"\n"
            if self.fail_midstream:
                raise urllib.error.URLError("stream reset")
            raise StopIteration

        def close(self):
            self.closed = True

    success_response = Response()
    calls = []

    def open_after_503(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "http://upstream.local",
                503,
                "busy",
                {"Retry-After": "0"},
                io.BytesIO(b'{"error":"busy"}'),
            )
        return success_response

    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="m")
    client.retry_initial_delay = 0
    client._opener = SimpleNamespace(open=open_after_503)
    events = list(client.stream("/v1/chat/completions", {"model": "m", "messages": []}))
    assert len(calls) == 2
    assert events and "hello" in events[0].data

    reset_response = Response(fail_midstream=True)
    reset_calls = []
    client._opener = SimpleNamespace(open=lambda *args, **kwargs: (reset_calls.append(1), reset_response)[1])
    generator = client.stream("/v1/chat/completions", {"model": "m", "messages": []})
    assert "hello" in next(generator).data
    with pytest.raises(UpstreamHTTPError):
        next(generator)
    assert len(reset_calls) == 1, "a stream must never replay after downstream-visible output"


def test_downstream_disconnect_closes_active_upstream_iterator():
    closed = threading.Event()

    class StreamingClient:
        def stream(self, path, body):
            try:
                yield _event({
                    "id": "disconnect",
                    "model": "m",
                    "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
                })
                yield _event({
                    "id": "disconnect",
                    "model": "m",
                    "choices": [{"index": 0, "delta": {"content": "later"}, "finish_reason": None}],
                })
            finally:
                closed.set()

    class BrokenHandler:
        class Writer:
            def write(self, data):
                if b"hello" in data:
                    raise BrokenPipeError("client disconnected")

            def flush(self):
                return

        wfile = Writer()

    with tempfile.TemporaryDirectory() as td:
        old_config = gateway.CONFIG_PATH
        gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
        try:
            cfg = _native_config(pathlib.Path(td))
            gateway.save_config(cfg)
            with pytest.raises(BrokenPipeError):
                _run_streaming_orchestration_scoped(
                    BrokenHandler(),
                    "/v1/chat/completions",
                    {"model": "m", "stream": True, "messages": [{"role": "user", "content": "say hello"}]},
                    mode="orchestrate",
                    upstream_protocol="openai_chat",
                    gateway_cfg=cfg["gateway"],
                    max_rounds=2,
                    upstream=StreamingClient(),
                    context_cfg={"enabled": False},
                )
            assert closed.wait(1)
        finally:
            gateway.CONFIG_PATH = old_config


def test_real_gateway_forwards_first_upstream_token_before_stream_completion():
    release_second = threading.Event()
    upstream_first_sent = threading.Event()
    seen_bodies = []

    class UpstreamHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
            seen_bodies.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(payload):
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                self.wfile.flush()

            send({
                "id": "real_stream",
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": "first-token"}, "finish_reason": None}],
            })
            upstream_first_sent.set()
            assert release_second.wait(5)
            send({
                "id": "real_stream",
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": " second-token"}, "finish_reason": None}],
            })
            send({
                "id": "real_stream",
                "model": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    with tempfile.TemporaryDirectory() as td:
        old_config = gateway.CONFIG_PATH
        gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
        gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
        gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
        gateway_thread.start()
        received_first = threading.Event()
        received = []
        reader_error = []
        try:
            cfg = _native_config(pathlib.Path(td))
            cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
            gateway.save_config(cfg)

            def read_gateway_stream():
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{gateway_server.server_address[1]}/v1/chat/completions",
                        data=json.dumps({
                            "model": "m",
                            "stream": True,
                            "messages": [{"role": "user", "content": "say hello"}],
                        }).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        while True:
                            line = response.readline()
                            if not line:
                                break
                            text = line.decode()
                            received.append(text)
                            if "first-token" in text:
                                received_first.set()
                except Exception as exc:
                    reader_error.append(exc)

            reader = threading.Thread(target=read_gateway_stream)
            reader.start()
            assert upstream_first_sent.wait(2)
            assert received_first.wait(2), "Gateway buffered the first token until upstream completion"
            assert reader.is_alive()
            release_second.set()
            reader.join(timeout=5)
            assert not reader.is_alive()
            assert not reader_error
            output = "".join(received)
            assert "first-token" in output and "second-token" in output
            assert output.index("first-token") < output.index("second-token")
            assert seen_bodies and seen_bodies[0]["stream"] is True
        finally:
            release_second.set()
            gateway_server.shutdown()
            gateway_server.server_close()
            gateway_thread.join(timeout=2)
            gateway.CONFIG_PATH = old_config
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)


def test_protocol_emitters_produce_terminal_events_without_duplicate_body():
    from src.gateway_stream_state import StreamDelta

    anthropic = CaptureHandler()
    anthropic_emitter = _DownstreamDeltaEmitter(anthropic, "/v1/messages")
    anthropic_emitter.emit(StreamDelta("text", "hello"), {"id": "msg_1", "model": "m", "usage": {}})
    assert anthropic_emitter.finish({
        "id": "msg_1",
        "model": "m",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "usage": {},
    })
    anthropic_text = anthropic.text()
    assert anthropic_text.count('"text": "hello"') == 1
    assert "message_start" in anthropic_text and "message_stop" in anthropic_text

    responses = CaptureHandler()
    responses_emitter = _DownstreamDeltaEmitter(responses, "/v1/responses")
    responses_emitter.emit(StreamDelta("text", "hello"), {"id": "resp_1", "model": "m", "created_at": 1})
    assert responses_emitter.finish({
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "model": "m",
        "output": [],
    })
    responses_text = responses.text()
    assert "response.output_text.delta" in responses_text
    assert "response.completed" in responses_text


def test_emitters_preserve_multiple_chat_choices_and_responses_items():
    from src.gateway_stream_state import StreamDelta

    chat = CaptureHandler()
    chat_emitter = _DownstreamDeltaEmitter(chat, "/v1/chat/completions", source_path="/v1/chat/completions")
    chat_emitter.emit(StreamDelta("text", "choice-zero", 0), {"id": "chat_multi", "model": "m"})
    chat_emitter.emit(StreamDelta("text", "choice-one", 1), {"id": "chat_multi", "model": "m"})
    assert chat_emitter.finish({
        "id": "chat_multi",
        "model": "m",
        "choices": [
            {"index": 0, "finish_reason": "stop"},
            {"index": 1, "finish_reason": "length"},
        ],
    })
    chat_text = chat.text()
    assert '"index": 0' in chat_text and '"index": 1' in chat_text
    assert '"finish_reason": "length"' in chat_text

    responses = CaptureHandler()
    responses_emitter = _DownstreamDeltaEmitter(responses, "/v1/responses", source_path="/v1/responses")
    responses_emitter.emit(StreamDelta("text", "first", 0, 0), {"id": "resp_multi", "model": "m"})
    responses_emitter.emit(StreamDelta("text", "second", 1, 0), {"id": "resp_multi", "model": "m"})
    assert responses_emitter.finish({"id": "resp_multi", "object": "response", "status": "completed", "output": []})
    responses_text = responses.text()
    assert responses_text.count("response.output_item.added") >= 2
    assert '"output_index": 1' in responses_text


def test_chat_upstream_streams_incrementally_to_anthropic_and_responses_clients():
    class ChatStreamingClient:
        def stream(self, path, body):
            yield _event({
                "id": "cross_stream",
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": "cross-protocol"}, "finish_reason": None}],
            })
            yield _event({
                "id": "cross_stream",
                "model": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })
            yield UpstreamSSEEvent(None, "[DONE]", 14)

    cases = [
        (
            "/v1/messages",
            {"model": "m", "stream": True, "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]},
            ("message_start", "content_block_delta", "message_stop"),
        ),
        (
            "/v1/responses",
            {"model": "m", "stream": True, "input": "hello"},
            ("response.created", "response.output_text.delta", "response.completed"),
        ),
    ]
    with tempfile.TemporaryDirectory() as td:
        old_config = gateway.CONFIG_PATH
        gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
        try:
            cfg = _native_config(pathlib.Path(td))
            gateway.save_config(cfg)
            for path, body, expected_events in cases:
                handler = CaptureHandler()
                _run_streaming_orchestration_scoped(
                    handler,
                    path,
                    body,
                    mode="orchestrate",
                    upstream_protocol="openai_chat",
                    gateway_cfg=cfg["gateway"],
                    max_rounds=2,
                    upstream=ChatStreamingClient(),
                    context_cfg={"enabled": False},
                )
                output = handler.text()
                assert "cross-protocol" in output
                for expected in expected_events:
                    assert expected in output
        finally:
            gateway.CONFIG_PATH = old_config
