from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import src.toolcall_gateway as gateway
from src.gateway_assistants import (
    AssistantAPIError,
    AssistantStore,
    cancel_run,
    create_assistant_response,
    create_run,
    create_thread_response,
    handle_assistants_request,
    get_assistant_store,
    reset_assistant_store,
    submit_tool_outputs,
)


@pytest.fixture(autouse=True)
def isolated_assistants_store(tmp_path, monkeypatch):
    old_config = gateway.CONFIG_PATH
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("GATEWAY_SQLITE_LOG_PATH", str(runtime / "gateway-log.sqlite3"))
    gateway.CONFIG_PATH = tmp_path / "gateway.config.json"
    gateway.save_config(gateway._default_config())
    reset_assistant_store()
    try:
        yield
    finally:
        reset_assistant_store()
        gateway.CONFIG_PATH = old_config


def test_gateway_owned_assistant_create_response_defaults_model(monkeypatch):
    monkeypatch.setenv("GATEWAY_UPSTREAM_MODEL", "fallback-model")
    response = create_assistant_response({"name": "probe", "instructions": "hi"})
    assert response["object"] == "assistant"
    assert response["id"].startswith("asst_")
    assert response["model"]
    assert response["name"] == "probe"
    assert response["tools"] == []


def test_gateway_owned_thread_create_response_does_not_echo_message_content():
    response = create_thread_response({"messages": [{"role": "user", "content": "secret"}], "metadata": {"tenant": "a"}})
    assert response["object"] == "thread"
    assert response["id"].startswith("thread_")
    assert response["metadata"] == {"tenant": "a"}
    assert response["gateway_message_count"] == 1
    assert "secret" not in json.dumps(response)


def test_assistants_and_threads_http_endpoints_are_gateway_owned_not_forwarded(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        old_config = gateway.CONFIG_PATH
        old_proxy_client = gateway.NativeProxyClient
        gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
        gateway.save_config(gateway._default_config())

        class ExplodingClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError("assistants/threads must not construct upstream client")

        gateway.NativeProxyClient = ExplodingClient
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            headers = {"authorization": "Bearer local-gateway-key", "content-type": "application/json"}
            for path, body, expected_object in [
                ("/v1/assistants", {"model": "m", "name": "probe"}, "assistant"),
                ("/v1/threads", {"messages": [{"role": "user", "content": "hi"}]}, "thread"),
            ]:
                req = urllib.request.Request(
                    base + path,
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    assert resp.status == 200
                    payload = json.loads(resp.read().decode("utf-8"))
                assert payload["object"] == expected_object
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            gateway.NativeProxyClient = old_proxy_client
            gateway.CONFIG_PATH = old_config


def _chat_response(content="done", *, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl_assistant_test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def test_assistants_threads_messages_persist_and_isolate_by_client():
    assistant = create_assistant_response({"model": "m", "name": "persistent"}, client_id="tenant-a")
    thread = create_thread_response(
        {"messages": [{"role": "user", "content": "hello"}]},
        client_id="tenant-a",
    )

    reset_assistant_store()
    retrieved = handle_assistants_request(
        "GET",
        f"/v1/assistants/{assistant['id']}",
        client_id="tenant-a",
    ).payload
    messages = handle_assistants_request(
        "GET",
        f"/v1/threads/{thread['id']}/messages",
        client_id="tenant-a",
        query={"order": ["asc"]},
    ).payload
    assert retrieved["name"] == "persistent"
    assert len(messages["data"]) == 1
    assert messages["data"][0]["content"][0]["text"]["value"] == "hello"

    with pytest.raises(AssistantAPIError) as exc_info:
        handle_assistants_request(
            "GET",
            f"/v1/assistants/{assistant['id']}",
            client_id="tenant-b",
        )
    assert exc_info.value.status == 404


def test_run_completes_and_persists_assistant_message_and_step():
    assistant = create_assistant_response(
        {"model": "m", "instructions": "Be concise"},
        client_id="tenant-a",
    )
    thread = create_thread_response(
        {"messages": [{"role": "user", "content": "question"}]},
        client_id="tenant-a",
    )
    seen = []

    def executor(request):
        seen.append(request)
        return _chat_response("answer")

    run = create_run(
        thread["id"],
        {"assistant_id": assistant["id"]},
        client_id="tenant-a",
        executor=executor,
    )
    assert run["status"] == "completed"
    assert run["completed_at"] is not None
    assert run["usage"]["total_tokens"] == 5
    assert seen[0]["messages"][0] == {"role": "system", "content": "Be concise"}

    messages = handle_assistants_request(
        "GET",
        f"/v1/threads/{thread['id']}/messages",
        client_id="tenant-a",
        query={"order": "asc"},
    ).payload["data"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[-1]["content"][0]["text"]["value"] == "answer"

    steps = handle_assistants_request(
        "GET",
        f"/v1/threads/{thread['id']}/runs/{run['id']}/steps",
        client_id="tenant-a",
    ).payload["data"]
    assert len(steps) == 1
    assert steps[0]["status"] == "completed"
    assert steps[0]["type"] == "message_creation"


def test_run_requires_action_and_submit_tool_outputs_resumes_to_completion():
    assistant = create_assistant_response(
        {
            "model": "m",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "lookup value",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        },
        client_id="tenant-a",
    )
    thread = create_thread_response(
        {"messages": [{"role": "user", "content": "lookup"}]},
        client_id="tenant-a",
    )
    call = {
        "id": "call_lookup",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }
    run = create_run(
        thread["id"],
        {"assistant_id": assistant["id"]},
        client_id="tenant-a",
        executor=lambda request: _chat_response("", tool_calls=[call]),
    )
    assert run["status"] == "requires_action"
    assert run["required_action"]["submit_tool_outputs"]["tool_calls"][0]["id"] == "call_lookup"

    resumed_requests = []

    def resumed(request):
        resumed_requests.append(request)
        return _chat_response("tool result accepted")

    completed = submit_tool_outputs(
        thread["id"],
        run["id"],
        {"tool_outputs": [{"tool_call_id": "call_lookup", "output": "42"}]},
        client_id="tenant-a",
        executor=resumed,
    )
    assert completed["status"] == "completed"
    assert any(message.get("role") == "tool" and message.get("content") == "42" for message in resumed_requests[0]["messages"])

    steps = handle_assistants_request(
        "GET",
        f"/v1/threads/{thread['id']}/runs/{run['id']}/steps",
        client_id="tenant-a",
        query={"order": "asc"},
    ).payload["data"]
    assert [step["status"] for step in steps] == ["completed", "completed"]
    assert steps[0]["step_details"]["tool_calls"][0]["output"] == "42"


def test_run_failure_is_terminal_and_cancel_closes_required_action():
    assistant = create_assistant_response({"model": "m"}, client_id="tenant-a")
    failed_thread = create_thread_response(
        {"messages": [{"role": "user", "content": "fail"}]},
        client_id="tenant-a",
    )

    def fail(_request):
        raise RuntimeError("upstream unavailable")

    failed = create_run(
        failed_thread["id"],
        {"assistant_id": assistant["id"]},
        client_id="tenant-a",
        executor=fail,
    )
    assert failed["status"] == "failed"
    assert failed["last_error"]["code"] == "gateway_run_failed"
    assert "upstream unavailable" in failed["last_error"]["message"]

    action_thread = create_thread_response(
        {"messages": [{"role": "user", "content": "tool"}]},
        client_id="tenant-a",
    )
    action = create_run(
        action_thread["id"],
        {"assistant_id": assistant["id"]},
        client_id="tenant-a",
        executor=lambda request: _chat_response(
            "",
            tool_calls=[{"id": "call_x", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        ),
    )
    cancelled = cancel_run(action_thread["id"], action["id"], client_id="tenant-a")
    assert cancelled["status"] == "cancelled"
    with pytest.raises(AssistantAPIError) as exc_info:
        submit_tool_outputs(
            action_thread["id"],
            action["id"],
            {"tool_outputs": [{"tool_call_id": "call_x", "output": "late"}]},
            client_id="tenant-a",
            executor=lambda request: _chat_response("bad"),
        )
    assert exc_info.value.status == 409


def test_thread_delete_cascades_messages_runs_and_steps():
    assistant = create_assistant_response({"model": "m"}, client_id="tenant-a")
    thread = create_thread_response(
        {"messages": [{"role": "user", "content": "hello"}]},
        client_id="tenant-a",
    )
    run = create_run(
        thread["id"],
        {"assistant_id": assistant["id"]},
        client_id="tenant-a",
        executor=lambda request: _chat_response("answer"),
    )
    deleted = handle_assistants_request(
        "DELETE",
        f"/v1/threads/{thread['id']}",
        client_id="tenant-a",
    ).payload
    assert deleted["deleted"] is True
    with pytest.raises(AssistantAPIError):
        handle_assistants_request(
            "GET",
            f"/v1/threads/{thread['id']}/runs/{run['id']}",
            client_id="tenant-a",
        )


def _http_json(base, path, *, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"authorization": "Bearer local-gateway-key", "content-type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_full_assistants_http_lifecycle(monkeypatch):
    import src.gateway_tool_runtime as runtime

    monkeypatch.setattr(runtime, "run_tool_orchestration", lambda path, body, client_id=None: _chat_response("http answer"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, assistant = _http_json(base, "/v1/assistants", method="POST", body={"model": "m"})
        assert status == 200
        status, assistant_list = _http_json(base, "/v1/assistants")
        assert status == 200 and assistant_list["data"][0]["id"] == assistant["id"]

        status, created_thread = _http_json(
            base,
            "/v1/threads",
            method="POST",
            body={"messages": [{"role": "user", "content": "http question"}]},
        )
        assert status == 200
        status, run = _http_json(
            base,
            f"/v1/threads/{created_thread['id']}/runs",
            method="POST",
            body={"assistant_id": assistant["id"]},
        )
        assert status == 200 and run["status"] == "completed"
        status, messages = _http_json(base, f"/v1/threads/{created_thread['id']}/messages?order=asc")
        assert status == 200
        assert [item["role"] for item in messages["data"]] == ["user", "assistant"]
        status, steps = _http_json(base, f"/v1/threads/{created_thread['id']}/runs/{run['id']}/steps")
        assert status == 200 and steps["data"][0]["type"] == "message_creation"
        status, deleted = _http_json(base, f"/v1/threads/{created_thread['id']}", method="DELETE")
        assert status == 200 and deleted["deleted"] is True
        status, _missing = _http_json(base, f"/v1/threads/{created_thread['id']}")
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_assistants_store_cleanup_bounds_age_rows_and_cascades_children(tmp_path):
    assistant = create_assistant_response({"model": "m"}, client_id="tenant-cleanup")
    created_thread = create_thread_response(
        {"messages": [{"role": "user", "content": "old question"}]},
        client_id="tenant-cleanup",
    )
    create_run(
        created_thread["id"],
        {"assistant_id": assistant["id"]},
        client_id="tenant-cleanup",
        executor=lambda request: _chat_response("old answer"),
    )
    store = get_assistant_store()
    with store._connection() as connection:
        for table in ("assistants", "threads", "messages", "runs", "run_steps"):
            connection.execute(f"UPDATE {table} SET updated_at=1")
        connection.commit()

    preview = store.cleanup(retention_days=1, dry_run=True, now=10 * 86400)
    assert preview["deleted"] == {
        "run_steps": 0,
        "runs": 0,
        "messages": 0,
        "threads": 0,
        "assistants": 0,
    }
    assert all(count >= 1 for count in preview["eligible"].values())

    applied = store.cleanup(retention_days=1, dry_run=False, now=10 * 86400)
    assert all(count == 0 for count in applied["rows"].values())
    assert sum(applied["deleted"].values()) >= 2

    bounded = AssistantStore(tmp_path / "bounded-assistants.sqlite3")
    for index in range(5):
        bounded.put_resource(
            "assistants",
            "tenant",
            {
                "id": f"asst_{index}",
                "object": "assistant",
                "created_at": int(time.time()),
            },
        )
    capacity = bounded.cleanup(
        retention_days=36500,
        max_rows=2,
        batch_size=2,
        max_batches=3,
        now=time.time(),
    )
    assert capacity["rows"]["assistants"] == 2
    assert capacity["deleted"]["assistants"] == 3


def test_workspace_metadata_key_overrides_service_env(monkeypatch):
    from src.gateway_tool_runtime import _request_workspace_root

    monkeypatch.setenv("GATEWAY_WORKSPACE_ROOT", "/service/workspace")
    body = {"metadata": {"workspace": "/client/workspace", "user_id": "tenant-a"}}
    assert str(_request_workspace_root(body)) == "/client/workspace"


def test_top_level_workspace_key_overrides_service_env(monkeypatch):
    from src.gateway_tool_runtime import _request_workspace_root

    monkeypatch.setenv("GATEWAY_WORKSPACE_ROOT", "/service/workspace")
    body = {"workspace": "/client/top-level"}
    assert str(_request_workspace_root(body)) == "/client/top-level"
