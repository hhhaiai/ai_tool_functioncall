#!/usr/bin/env python3
"""Strict Agent Planner protocol smoke.

This is the repeatable version of the live protocol check: every public
conversation protocol must enter the outer Agent Planner before a chat-only
upstream synthesizes text.  It uses a fake OpenAI-chat upstream so the smoke is
deterministic and does not depend on the real Mimo endpoint.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.toolcall_gateway as gateway
from src.gateway_persistence import PersistenceConfig, init_persistence

Json = dict[str, Any]


class UpstreamHandler(BaseHTTPRequestHandler):
    seen: list[Json] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        UpstreamHandler.seen.append({"path": self.path, "body": body})
        idx = len(UpstreamHandler.seen)
        payload = json.dumps(
            {
                "id": f"chatcmpl_protocol_{idx}",
                "object": "chat.completion",
                "model": body.get("model") or "strict-fake",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"strict protocol ok {idx}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # noqa: N802
        return


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _post_json(base_url: str, path: str, body: Json, *, stream: bool = False) -> Json | str:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"authorization": "Bearer local-gateway-key", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        if stream:
            _assert("text/event-stream" in resp.headers.get("content-type", ""), f"{path}: not SSE")
            return raw
        return json.loads(raw)


def _response_context(response: Json) -> Json:
    ctx = response.get("gateway_context")
    return ctx if isinstance(ctx, dict) else {}


def _intent_kind(response: Json) -> str:
    ctx = _response_context(response)
    agent = ctx.get("agent_planner") if isinstance(ctx.get("agent_planner"), dict) else {}
    intent = agent.get("intent") if isinstance(agent.get("intent"), dict) else {}
    return str(intent.get("kind") or "")


def _admin_json(base_url: str, path: str) -> Json:
    token = base64.b64encode(b"admin:admin").decode("ascii")
    req = urllib.request.Request(f"{base_url}{path}", headers={"authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    run_root = pathlib.Path(".gateway_runtime") / f"agent-planner-protocol-strict-{time.strftime('%Y%m%d-%H%M%S')}"
    run_root.mkdir(parents=True, exist_ok=True)
    old_config = gateway.CONFIG_PATH
    old_ready = gateway.SQLITE_READY
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
        gateway.CONFIG_PATH = run_root / "gateway.config.json"
        os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(run_root / "gateway_log.sqlite3")
        os.environ["GATEWAY_RUNTIME_DIR"] = str(run_root / "runtime")
        os.environ["GATEWAY_WORKSPACE_ROOT"] = str((run_root / "workspace").resolve())
        os.environ["GATEWAY_UPSTREAM_STREAM_AGGREGATE"] = "0"
        gateway.SQLITE_READY = False

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        cfg = gateway._default_config()
        cfg["gateway"]["tool_mode"] = "orchestrate"
        cfg["gateway"]["agent_planner_strict_every_turn"] = True
        cfg["gateway"]["local_planner_enabled"] = False
        cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
        cfg["upstream"]["model"] = "strict-fake"
        cfg["upstream"]["protocol"] = "openai_chat"
        cfg["upstream"]["tools_enabled"] = "adapter"
        cfg["upstream"]["capabilities"]["supports_tools"] = False
        cfg["upstream"]["capabilities"]["supports_function_calls"] = False
        gateway.save_config(cfg)
        init_persistence(PersistenceConfig(db_path=str(run_root / "gateway.db")))

        gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
        gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
        gateway_thread.start()
        base_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"
        tenant = "protocol-strict-user"

        canonical_non_stream_specs = [
            (
                "/v1/chat/completions",
                {"model": "downstream", "metadata": {"session_id": "proto-chat", "user_id": tenant}, "messages": [{"role": "user", "content": "hi chat"}]},
            ),
            (
                "/v1/responses",
                {"model": "downstream", "metadata": {"session_id": "proto-responses", "user_id": tenant}, "input": "hi responses"},
            ),
            (
                "/v1/messages",
                {
                    "model": "downstream",
                    "metadata": {"session_id": "proto-messages", "user_id": tenant},
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi messages"}]}],
                    "max_tokens": 128,
                },
            ),
        ]
        alias_non_stream_specs = [
            (path.replace("/v1/", "/anthropic/v1/"), {**payload, "metadata": {**payload["metadata"], "session_id": f"{payload['metadata']['session_id']}-alias"}})
            for path, payload in canonical_non_stream_specs
        ]
        non_stream_specs = canonical_non_stream_specs + alias_non_stream_specs
        non_stream_results: list[Json] = []
        for path, payload in non_stream_specs:
            response = _post_json(base_url, path, payload)
            _assert(isinstance(response, dict), f"{path}: non-json response")
            ctx = _response_context(response)
            _assert(ctx.get("agent_planner_strict_every_turn") is True, f"{path}: strict context missing")
            _assert(ctx.get("strategy") == "agent_planner_final_synthesis", f"{path}: synthesis strategy missing")
            _assert(_intent_kind(response) == "plain_chat", f"{path}: wrong intent {_intent_kind(response)}")
            non_stream_results.append(response)

        canonical_stream_specs = [
            (
                "/v1/chat/completions",
                {"model": "downstream", "stream": True, "metadata": {"session_id": "proto-chat-stream", "user_id": tenant}, "messages": [{"role": "user", "content": "hi chat stream"}]},
            ),
            (
                "/v1/responses",
                {"model": "downstream", "stream": True, "metadata": {"session_id": "proto-responses-stream", "user_id": tenant}, "input": "hi responses stream"},
            ),
            (
                "/v1/messages",
                {
                    "model": "downstream",
                    "stream": True,
                    "metadata": {"session_id": "proto-messages-stream", "user_id": tenant},
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi messages stream"}]}],
                    "max_tokens": 128,
                },
            ),
        ]
        alias_stream_specs = [
            (path.replace("/v1/", "/anthropic/v1/"), {**payload, "metadata": {**payload["metadata"], "session_id": f"{payload['metadata']['session_id']}-alias"}})
            for path, payload in canonical_stream_specs
        ]
        stream_specs = canonical_stream_specs + alias_stream_specs
        stream_payloads: list[str] = []
        for path, payload in stream_specs:
            sse = _post_json(base_url, path, payload, stream=True)
            _assert(isinstance(sse, str) and ("data:" in sse or "event:" in sse), f"{path}: SSE data missing")
            _assert("[DONE]" in sse or "response.completed" in sse or "message_stop" in sse, f"{path}: SSE completion missing")
            stream_payloads.append(sse)

        _assert(len(UpstreamHandler.seen) == 12, f"upstream call count drifted: {len(UpstreamHandler.seen)}")
        for item in UpstreamHandler.seen:
            body = item["body"]
            serialized_body = json.dumps(body, ensure_ascii=False)
            _assert("gateway_context" not in body, "upstream payload leaked gateway_context")
            _assert("gateway_agent_planner" not in body, "upstream payload leaked gateway_agent_planner")
            _assert("Gateway Agent Planner evidence/envelope" in serialized_body, "upstream payload missing planner synthesis prompt")
            _assert("tools" not in body and "tool_choice" not in body, "upstream payload leaked tool surface")

        qs = urllib.parse.urlencode({"limit": "120", "tenant_contains": tenant})
        audit = _admin_json(base_url, f"/admin/agent-runtime-audit.json?{qs}")["audit"]
        requirements = audit["requirements"]
        strict = requirements["strict_every_turn_planner_envelope"]
        parity = requirements["streaming_nonstreaming_parity"]
        _assert(strict["status"] == "proven/current_scope", f"strict audit failed: {strict}")
        _assert(strict["detail"]["missing_session_count"] == 0, f"strict missing sessions: {strict}")
        _assert(strict["detail"]["covered_session_count"] == 12, f"strict covered session drifted: {strict}")
        _assert(parity["status"] == "proven/current_scope", f"streaming parity audit failed: {parity}")
        _assert({"streaming", "non_streaming"}.issubset(set(parity["detail"]["seen_synthesis_sources"])), f"parity sources drifted: {parity}")

        print(json.dumps({
            "ok": True,
            "run_dir": str(run_root.resolve()),
            "non_stream_paths": [path for path, _ in non_stream_specs],
            "stream_paths": [path for path, _ in stream_specs],
            "upstream_calls": len(UpstreamHandler.seen),
            "alias_paths_checked": True,
            "strict_sessions": strict["detail"],
            "parity": parity["detail"],
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
        gateway.SQLITE_READY = old_ready
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
