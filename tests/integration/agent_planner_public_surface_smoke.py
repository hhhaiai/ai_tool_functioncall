#!/usr/bin/env python3
"""Public API surface smoke for the remote Agent Planner gateway.

The health endpoint is an operator/client contract.  Every advertised path must
be callable without 5xx, including /anthropic/v1/* aliases, and strict planner
conversation paths must still enter the outer Agent Planner.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.toolcall_gateway as gateway
from src.gateway_config import WEB2API_PATHS, _normalize_request_path
from src.gateway_http_handler import _gateway_is_ready, _set_gateway_ready
from src.gateway_persistence import PersistenceConfig, init_persistence

Json = dict[str, Any]


class PublicSurfaceUpstream(BaseHTTPRequestHandler):
    seen: list[Json] = []
    fail_models: bool = False
    web2api_url: str = ""

    def do_GET(self):  # noqa: N802
        if self.path == "/web2api-page":
            raw = b"<html><head><title>Surface Web2API</title></head><body><h1>web2api ok</h1></body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.endswith("/models"):
            if PublicSurfaceUpstream.fail_models:
                self._json(503, {"error": {"message": "models temporarily unavailable"}})
                return
            self._json(200, {"object": "list", "data": [{"id": "surface-fake", "object": "model"}]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        PublicSurfaceUpstream.seen.append({"path": self.path, "body": body})
        idx = len(PublicSurfaceUpstream.seen)
        self._json(
            200,
            {
                "id": f"chatcmpl_surface_{idx}",
                "object": "chat.completion",
                "model": body.get("model") or "surface-fake",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"surface ok {idx}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    def _json(self, status: int, payload: Json) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # noqa: N802
        return


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _request_json(base_url: str, path: str, *, method: str = "GET", body: Json | None = None) -> tuple[int, Json]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers={"authorization": "Bearer local-gateway-key", "content-type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def _admin_json(base_url: str, path: str) -> Json:
    token = base64.b64encode(b"admin:admin").decode("ascii")
    req = urllib.request.Request(f"{base_url}{path}", headers={"authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _payload_for(path: str, tenant: str, index: int, client_workspace: pathlib.Path) -> tuple[str, Json | None, str]:
    canonical = _normalize_request_path(path)
    session = f"surface-{index}-{canonical.strip('/').replace('/', '-') or 'root'}"
    metadata = {"session_id": session, "user_id": tenant, "workspace": str(client_workspace)}
    if canonical == "/v1/models":
        return "GET", None, "models"
    if canonical == "/v1/chat/completions":
        return "POST", {"model": "surface", "metadata": metadata, "messages": [{"role": "user", "content": "hi chat"}]}, "conversation"
    if canonical == "/v1/responses":
        return "POST", {"model": "surface", "metadata": metadata, "input": "hi responses"}, "conversation"
    if canonical == "/v1/messages":
        return "POST", {"model": "surface", "metadata": metadata, "messages": [{"role": "user", "content": "hi messages"}], "max_tokens": 64}, "conversation"
    if canonical == "/v1/assistants":
        return "POST", {"model": "surface", "name": "surface assistant", "metadata": metadata}, "assistant"
    if canonical == "/v1/threads":
        return "POST", {"messages": [{"role": "user", "content": "hi thread"}], "metadata": metadata}, "thread"
    if canonical in {"/v1/messages/count_tokens", "/v1/chat/completions/count_tokens"}:
        return "POST", {"metadata": metadata, "messages": [{"role": "user", "content": "hello token count"}]}, "count_tokens"
    if canonical in {"/v1/tools/call", "/v1/functions/call", "/tools/call"}:
        return "POST", {
            "metadata": metadata,
            "function": {"name": "calculator", "arguments": json.dumps({"expression": "20+22"})},
            "call_id": session,
        }, "direct_tool"
    if canonical in WEB2API_PATHS or path in WEB2API_PATHS:
        return "POST", {
            "url": PublicSurfaceUpstream.web2api_url,
            "selectors": {"heading": "h1"},
        }, "web2api"
    raise AssertionError(f"unhandled advertised path: {path} -> {canonical}")


def main() -> None:
    run_root = pathlib.Path(".gateway_runtime") / f"agent-planner-public-surface-{time.strftime('%Y%m%d-%H%M%S')}"
    run_root.mkdir(parents=True, exist_ok=True)
    old_config = gateway.CONFIG_PATH
    old_ready = _gateway_is_ready()
    old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
    old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
    old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    old_stream_aggregate = os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE")
    planner = None
    upstream = None
    gateway_server = None
    upstream_thread = None
    gateway_thread = None
    try:
        import src.gateway_agent_planner as planner

        planner._STORE = None
        PublicSurfaceUpstream.seen = []
        gateway.CONFIG_PATH = run_root / "gateway.config.json"
        os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(run_root / "gateway_log.sqlite3")
        os.environ["GATEWAY_RUNTIME_DIR"] = str(run_root / "runtime")
        service_workspace = (run_root / "service-workspace").resolve()
        client_workspace = (run_root / "client-workspace").resolve()
        service_workspace.mkdir(parents=True, exist_ok=True)
        client_workspace.mkdir(parents=True, exist_ok=True)
        (service_workspace / "surface-client.txt").write_text("SERVICE_WORKSPACE_SHOULD_NOT_BE_USED\n", encoding="utf-8")
        (client_workspace / "surface-client.txt").write_text("CLIENT_WORKSPACE_OK\n", encoding="utf-8")
        os.environ["GATEWAY_WORKSPACE_ROOT"] = str(service_workspace)
        os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = "0"
        _set_gateway_ready(True)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), PublicSurfaceUpstream)
        PublicSurfaceUpstream.web2api_url = f"http://127.0.0.1:{upstream.server_address[1]}/web2api-page"
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        cfg = gateway._default_config()
        cfg["gateway"]["tool_mode"] = "orchestrate"
        cfg["gateway"]["agent_planner_strict_every_turn"] = True
        cfg["gateway"]["local_planner_enabled"] = False
        cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
        cfg["upstream"]["model"] = "surface-fake"
        cfg["upstream"]["protocol"] = "openai_chat"
        cfg["upstream"]["tools_enabled"] = "adapter"
        cfg["upstream"]["capabilities"]["supports_tools"] = False
        cfg["upstream"]["capabilities"]["supports_function_calls"] = False
        cfg["web2api"]["allow_private_network"] = True
        gateway.save_config(cfg)
        init_persistence(PersistenceConfig(db_path=str(run_root / "gateway.db")))

        gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
        gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
        gateway_thread.start()
        base_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"
        tenant = "surface-user"

        health_status, health = _request_json(base_url, "/healthz")
        _assert(health_status == 200 and health.get("ok") is True, f"health failed: {health_status} {health}")
        advertised = sorted(health.get("supported_paths") or [])
        _assert(advertised, "health did not advertise supported_paths")

        results: list[Json] = []
        for index, path in enumerate(advertised):
            method, body, kind = _payload_for(path, tenant, index, client_workspace)
            request_path = path
            if kind == "models":
                request_path = (
                    f"{path}?tenant={urllib.parse.quote(tenant)}"
                    f"&workspace={urllib.parse.quote(str(client_workspace))}"
                    f"&session_id={urllib.parse.quote(f'surface-{index}-models')}"
                )
            status, payload = _request_json(base_url, request_path, method=method, body=body)
            _assert(status < 500, f"{method} {path} returned server error {status}: {payload}")
            _assert(status in {200, 201}, f"{method} {path} returned unexpected status {status}: {payload}")
            canonical = _normalize_request_path(path)
            if kind == "conversation":
                ctx = payload.get("gateway_context") if isinstance(payload.get("gateway_context"), dict) else {}
                agent = ctx.get("agent_planner") if isinstance(ctx.get("agent_planner"), dict) else {}
                intent = agent.get("intent") if isinstance(agent.get("intent"), dict) else {}
                _assert(ctx.get("agent_planner_strict_every_turn") is True, f"{path}: strict context missing")
                _assert(intent.get("kind") == "plain_chat", f"{path}: wrong planner intent {intent}")
            elif kind == "assistant":
                _assert(payload.get("object") == "assistant" and str(payload.get("id") or "").startswith("asst_"), f"{path}: bad assistant payload {payload}")
            elif kind == "thread":
                _assert(payload.get("object") == "thread" and str(payload.get("id") or "").startswith("thread_"), f"{path}: bad thread payload {payload}")
            elif kind == "direct_tool":
                _assert(payload.get("object") == "gateway.tool_result" and payload.get("success") is True, f"{path}: bad direct tool payload {payload}")
                content = str(payload.get("content") or "")
                _assert(content == "42", f"{path}: direct gateway-owned tool returned wrong content: {payload}")
            elif kind == "models":
                _assert(payload.get("object") == "list", f"{path}: bad models payload {payload}")
            elif kind == "count_tokens":
                _assert(isinstance(payload.get("input_tokens"), int), f"{path}: bad count_tokens payload {payload}")
            elif kind == "web2api":
                _assert(payload.get("object") == "gateway.web2api.result", f"{path}: bad Web2API payload {payload}")
                extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
                _assert(extracted.get("heading") == "web2api ok", f"{path}: wrong Web2API extraction {payload}")
            results.append({"path": path, "canonical": canonical, "method": method, "status": status, "kind": kind})

        invalid_status, invalid_payload = _request_json(
            base_url,
            "/v1/tools/call",
            method="POST",
            body={
                "metadata": {"session_id": "surface-invalid-direct", "user_id": tenant, "workspace": str(client_workspace)},
                "arguments": {"file_path": "surface-client.txt"},
            },
        )
        _assert(invalid_status == 400, f"invalid direct tool input should be 400, got {invalid_status}: {invalid_payload}")
        invalid_error = invalid_payload.get("error") if isinstance(invalid_payload.get("error"), dict) else {}
        _assert(invalid_error.get("detail", {}).get("failure_type") == "invalid_input", f"invalid direct error detail drifted: {invalid_payload}")

        user_side_status, user_side_payload = _request_json(
            base_url,
            "/v1/tools/call",
            method="POST",
            body={
                "metadata": {"session_id": "surface-user-side-direct", "user_id": tenant, "workspace": str(client_workspace)},
                "function": {"name": "Read", "arguments": json.dumps({"file_path": "surface-client.txt"})},
                "call_id": "surface-user-side-direct",
            },
        )
        _assert(user_side_status == 400, f"user-side direct tool should be 400 in cloud mode, got {user_side_status}: {user_side_payload}")
        user_side_error = user_side_payload.get("error") if isinstance(user_side_payload.get("error"), dict) else {}
        _assert(
            user_side_error.get("detail", {}).get("failure_type") == "direct_user_side_tool_requires_downstream_client",
            f"user-side direct error detail drifted: {user_side_payload}",
        )
        serialized_user_side_error = json.dumps(user_side_payload, ensure_ascii=False)
        _assert("CLIENT_WORKSPACE_OK" not in serialized_user_side_error, f"user-side direct error leaked client file content: {user_side_payload}")
        _assert("SERVICE_WORKSPACE_SHOULD_NOT_BE_USED" not in serialized_user_side_error, f"user-side direct error leaked service file content: {user_side_payload}")

        PublicSurfaceUpstream.fail_models = True
        model_error_path = (
            f"/v1/models?tenant={urllib.parse.quote(tenant)}"
            f"&workspace={urllib.parse.quote(str(client_workspace))}"
            f"&session_id={urllib.parse.quote('surface-models-error')}"
        )
        model_error_status, model_error_payload = _request_json(base_url, model_error_path)
        PublicSurfaceUpstream.fail_models = False
        _assert(model_error_status == 502, f"models upstream failure should be 502, got {model_error_status}: {model_error_payload}")

        audit = _admin_json(base_url, "/admin/agent-runtime-audit.json?limit=120&tenant_contains=surface-user")["audit"]
        strict = audit["requirements"]["strict_every_turn_planner_envelope"]
        gateway_owned = audit["requirements"]["gateway_owned_service_tools"]
        _assert(strict["status"] == "proven/current_scope", f"strict audit failed: {strict}")
        _assert(strict["detail"]["missing_session_count"] == 0, f"strict audit missing sessions: {strict}")
        _assert(gateway_owned["status"] == "proven/current_scope", f"gateway-owned direct tool audit failed: {gateway_owned}")

        direct_result_events = _admin_json(
            base_url,
            "/admin/agent-runtime-events.json?limit=80&tenant_contains=surface-user&event_type=direct_tool_result",
        )["events"]
        direct_error_events = _admin_json(
            base_url,
            "/admin/agent-runtime-events.json?limit=20&tenant_contains=surface-user&event_type=direct_tool_error",
        )["events"]
        token_count_events = _admin_json(
            base_url,
            "/admin/agent-runtime-events.json?limit=20&tenant_contains=surface-user&event_type=token_count_result",
        )["events"]
        model_events = _admin_json(
            base_url,
            "/admin/agent-runtime-events.json?limit=20&tenant_contains=surface-user&event_type=models_result",
        )["events"]
        model_error_events = _admin_json(
            base_url,
            "/admin/agent-runtime-events.json?limit=20&tenant_contains=surface-user&event_type=models_error",
        )["events"]
        assistant_events = _admin_json(
            base_url,
            "/admin/agent-runtime-events.json?limit=20&tenant_contains=surface-user&event_type=assistants_result",
        )["events"]
        thread_events = _admin_json(
            base_url,
            "/admin/agent-runtime-events.json?limit=20&tenant_contains=surface-user&event_type=threads_result",
        )["events"]
        # The HTTP handler normalizes /anthropic/v1/* aliases before runtime
        # recording, so event session keys are canonical even though all alias
        # paths are exercised above.  Count still proves all five direct public
        # paths produced result events.
        expected_direct_paths = {"/v1/functions/call", "/v1/tools/call", "/tools/call"}
        seen_direct_paths = {str(event.get("session_key") or "").split(":", 1)[0] for event in direct_result_events}
        _assert(len(direct_result_events) == 5, f"wrong direct result event count: {len(direct_result_events)}")
        _assert(len(direct_error_events) == 2, f"wrong direct error event count: {len(direct_error_events)}")
        _assert(len(token_count_events) == 4, f"wrong token count event count: {len(token_count_events)}")
        _assert(len(model_events) == 2, f"wrong model list event count: {len(model_events)}")
        _assert(len(model_error_events) == 1, f"wrong model error event count: {len(model_error_events)}")
        _assert(len(assistant_events) == 2, f"wrong assistant event count: {len(assistant_events)}")
        _assert(len(thread_events) == 2, f"wrong thread event count: {len(thread_events)}")
        _assert(expected_direct_paths.issubset(seen_direct_paths), f"direct tool runtime events missing canonical paths: {seen_direct_paths}")
        error_failure_types = set()
        for event in direct_error_events:
            error_metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            error_failure_types.add(error_metadata.get("failure_type"))
            _assert("client-workspace" in str(event.get("workspace_key") or ""), f"direct error event not client-workspace scoped: {event}")
        _assert(
            {"invalid_input", "direct_user_side_tool_requires_downstream_client"}.issubset(error_failure_types),
            f"bad direct error event metadata: {direct_error_events}",
        )
        for event in token_count_events:
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            _assert(metadata.get("source") == "token_count_endpoint", f"bad token count event source: {event}")
            _assert(metadata.get("success") is True, f"token count event not successful: {event}")
            _assert(isinstance(metadata.get("input_tokens"), int), f"token count event missing token value: {event}")
            _assert("client-workspace" in str(event.get("workspace_key") or ""), f"token count event not client-workspace scoped: {event}")
        for event in model_events + assistant_events + thread_events:
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            _assert(metadata.get("owner") == "gateway_service", f"bad gateway-owned event owner: {event}")
            _assert(metadata.get("success") is True, f"gateway-owned event not successful: {event}")
            _assert("client-workspace" in str(event.get("workspace_key") or ""), f"gateway-owned event not client-workspace scoped: {event}")
        model_error_metadata = model_error_events[0].get("metadata") if isinstance(model_error_events[0].get("metadata"), dict) else {}
        _assert(model_error_metadata.get("owner") == "gateway_service", f"bad models_error owner: {model_error_events[0]}")
        _assert(model_error_metadata.get("success") is False, f"models_error should be unsuccessful: {model_error_events[0]}")
        _assert("failure_type" in model_error_metadata, f"models_error missing failure_type: {model_error_events[0]}")
        _assert("client-workspace" in str(model_error_events[0].get("workspace_key") or ""), f"models_error not client-workspace scoped: {model_error_events[0]}")
        for event in direct_result_events:
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            _assert(metadata.get("source") == "direct_tool_endpoint", f"bad direct event source: {event}")
            _assert(metadata.get("success") is True, f"direct result event not successful: {event}")
            _assert("client-workspace" in str(event.get("workspace_key") or ""), f"direct event not client-workspace scoped: {event}")

        print(json.dumps({
            "ok": True,
            "run_dir": str(run_root.resolve()),
            "advertised_count": len(advertised),
            "checked": results,
            "upstream_calls": len(PublicSurfaceUpstream.seen),
            "strict_sessions": strict["detail"],
            "gateway_owned_service_tools": gateway_owned["status"],
            "direct_tool_result_event_count": len(direct_result_events),
            "direct_tool_error_event_count": len(direct_error_events),
            "token_count_result_event_count": len(token_count_events),
            "models_result_event_count": len(model_events),
            "models_error_event_count": len(model_error_events),
            "assistants_result_event_count": len(assistant_events),
            "threads_result_event_count": len(thread_events),
            "direct_tool_event_paths": sorted(seen_direct_paths),
        }, ensure_ascii=False, indent=2))
    finally:
        if gateway_server is not None:
            gateway_server.shutdown()
            gateway_server.server_close()
        if upstream is not None:
            upstream.shutdown()
            upstream.server_close()
        if gateway_thread is not None:
            gateway_thread.join(timeout=2)
        if upstream_thread is not None:
            upstream_thread.join(timeout=2)
        gateway.CONFIG_PATH = old_config
        _set_gateway_ready(old_ready)
        if old_sqlite is None:
            os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
        else:
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = old_sqlite
        if old_runtime is None:
            os.environ.pop("GATEWAY_RUNTIME_DIR", None)
        else:
            os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime
        if old_ws is None:
            os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
        else:
            os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws
        if old_stream_aggregate is None:
            os.environ.pop("GATEWAY_UPSTREAM_STREAM_AGGREGATE", None)
        else:
            os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = old_stream_aggregate
        gateway._mcp_close_sessions()
        if planner is not None:
            planner._STORE = None


if __name__ == "__main__":
    main()
