#!/usr/bin/env python3
"""Remote Agent Planner pressure smoke.

Covers the goal-specific boundary that this service is a remote Agent Runtime:
multiple tenants/workspaces can concurrently ask for downstream tool dispatch,
conversation memory rolls up per tenant/workspace/session, and recalled memory is
re-injected without leaking another client's workspace or markers.
"""
from __future__ import annotations

import concurrent.futures
import base64
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.toolcall_gateway as gateway
from src.gateway_streaming import run_streaming_orchestration
from src.gateway_tool_runtime import run_tool_orchestration

Json = dict[str, Any]


class FakeClient:
    def __init__(self, responses: list[Json] | None = None):
        self.responses = list(responses or [])
        self.requests: list[tuple[str, Json]] = []

    def forward(self, path: str, body: Json) -> Json:
        self.requests.append((path, body))
        if self.responses:
            return self.responses.pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}


class FakeStreamingHandler:
    def __init__(self):
        self.events: list[str] = []

        outer = self

        class WFile:
            @staticmethod
            def write(data):
                outer.events.append(data.decode("utf-8") if isinstance(data, bytes) else str(data))

            @staticmethod
            def flush():
                pass

        self.wfile = WFile()

    def send_response(self, status):
        self.events.append(f"STATUS:{status}\n")

    def send_header(self, key, value):
        self.events.append(f"HEADER:{key}:{value}\n")

    def end_headers(self):
        self.events.append("END_HEADERS\n")


def _chat_response(text: str) -> Json:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _metadata(user: str) -> Json:
    return {"session_id": f"pressure-session-{user}", "user_id": json.dumps({"user_id": user})}


def _read_body(user: str, root: pathlib.Path) -> Json:
    return {
        "model": "weak",
        # This smoke models a remote Gateway receiving a downstream client
        # workspace.  Pass the client path explicitly/canonically so a relative
        # test harness path is not interpreted as a Gateway-service cwd target.
        "workspace_root": str(root.resolve()),
        "metadata": _metadata(user),
        "messages": [{"role": "user", "content": "请读取 README.md"}],
        "tools": [{
            "name": "Read",
            "input_schema": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
                "additionalProperties": False,
            },
        }],
        "max_tokens": 128,
    }


def _chat_body(user: str, root: pathlib.Path, text: str) -> Json:
    return {
        "model": "weak",
        "workspace_root": str(root.resolve()),
        "metadata": _metadata(user),
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 128,
    }


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    run_root = pathlib.Path(".gateway_runtime") / f"agent-planner-remote-pressure-{time.strftime('%Y%m%d-%H%M%S')}"
    run_root.mkdir(parents=True, exist_ok=True)
    old_config = gateway.CONFIG_PATH
    old_ready = gateway.SQLITE_READY
    old_sqlite = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
    old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
    old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    planner = None
    try:
        import src.gateway_agent_planner as planner

        planner._STORE = None
        gateway.CONFIG_PATH = run_root / "gateway.config.json"
        os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(run_root / "gateway_log.sqlite3")
        os.environ["GATEWAY_RUNTIME_DIR"] = str(run_root / "runtime")
        os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
        gateway.SQLITE_READY = False
        cfg = gateway._default_config()
        cfg["gateway"]["tool_mode"] = "orchestrate"
        cfg["gateway"]["agent_planner_strict_every_turn"] = True
        cfg["gateway"]["local_planner_enabled"] = False
        cfg["context"]["memory_enabled"] = True
        cfg["context"]["memory_rollup_every_turns"] = 2
        cfg["context"]["memory_rollup_max_chars"] = 1600
        cfg["context"]["memory_inject_max_chars"] = 4000
        cfg["upstream"]["tools_enabled"] = "adapter"
        cfg["upstream"]["capabilities"]["supports_tools"] = False
        cfg["upstream"]["capabilities"]["supports_function_calls"] = False
        gateway.save_config(cfg)

        users = [f"pressure-user-{idx}" for idx in range(6)]
        workspaces: dict[str, pathlib.Path] = {}
        for user in users:
            root = run_root / "clients" / user
            root.mkdir(parents=True)
            marker = f"README marker for {user}"
            (root / "README.md").write_text(marker + "\n", encoding="utf-8")
            workspaces[user] = root

        def read_worker(user: str) -> Json:
            root = workspaces[user]
            client = FakeClient([])
            result = run_tool_orchestration("/v1/messages", _read_body(user, root), client)
            _assert(client.requests == [], f"{user}: downstream read should not call upstream")
            serialized = json.dumps(result, ensure_ascii=False)
            expected_path = str((root / "README.md").resolve())
            _assert(expected_path in serialized, f"{user}: missing own read path")
            for other_user, other_root in workspaces.items():
                if other_user != user:
                    _assert(str((other_root / "README.md").resolve()) not in serialized, f"{user}: leaked {other_user} path")
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(users)) as executor:
            read_results = list(executor.map(read_worker, users))
        _assert(len(read_results) == len(users), "not all read workers completed")

        def remember_worker(user: str) -> None:
            root = workspaces[user]
            alpha = f"ALPHA-{user}"
            beta = f"BETA-{user}"
            for marker in (alpha, beta):
                client = FakeClient([_chat_response(f"Recorded {marker}")])
                run_tool_orchestration(
                    "/v1/chat/completions",
                    _chat_body(user, root, f"Remember remote pressure marker {marker}"),
                    client,
                )
                _assert(client.requests, f"{user}: chat turn should call upstream")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(users)) as executor:
            list(executor.map(remember_worker, users))

        recall_payloads: dict[str, str] = {}
        for user in users:
            root = workspaces[user]
            client = FakeClient([_chat_response(f"Recall complete for {user}")])
            run_tool_orchestration(
                "/v1/chat/completions",
                _chat_body(user, root, "What remote pressure markers did we record earlier?"),
                client,
            )
            _assert(client.requests, f"{user}: recall should call upstream")
            payload = json.dumps(client.requests[0][1], ensure_ascii=False)
            recall_payloads[user] = payload
            _assert("Gateway recalled memory" in payload, f"{user}: recalled memory missing")
            _assert(f"ALPHA-{user}" in payload or f"BETA-{user}" in payload, f"{user}: own marker missing")
            for other in users:
                if other != user:
                    _assert(f"ALPHA-{other}" not in payload and f"BETA-{other}" not in payload, f"{user}: leaked memory from {other}")

        for user, root in workspaces.items():
            workspace_key = str(root.resolve())
            sessions = planner._store().list_recent(20, tenant_contains=user, workspace_contains=workspace_key)
            _assert(sessions, f"{user}: missing planner session")
            events = planner.list_runtime_events(100, tenant_contains=user, workspace_contains=workspace_key)
            event_types = {event.get("event_type") for event in events}
            _assert("tool_dispatch" in event_types, f"{user}: missing tool_dispatch event")
            memories = gateway._sqlite_tail_memories(50, tenant_contains=user, workspace_contains=workspace_key, session_contains=f"pressure-session-{user}")
            _assert(any(mem.get("kind") == "session_rollup" for mem in memories), f"{user}: missing session_rollup")
            summaries = json.dumps(memories, ensure_ascii=False)
            for other in users:
                if other != user:
                    _assert(f"ALPHA-{other}" not in summaries and f"BETA-{other}" not in summaries, f"{user}: memory DB leak from {other}")

        admin_user = users[0]
        admin_workspace = str(workspaces[admin_user].resolve())

        # Exercise a Gateway-owned service tool in the same remote scope before
        # querying admin audit APIs.  This proves the split the user cares
        # about: user-machine tools still dispatch to the downstream client
        # workspace, while pure service tools can run in the remote Gateway and
        # then hand only compact evidence to the chat-only upstream.
        calc_client = FakeClient([_chat_response(f"Calculator synthesis for {admin_user}")])
        run_tool_orchestration(
            "/v1/chat/completions",
            _chat_body(admin_user, workspaces[admin_user], "请计算 21 + 21"),
            calc_client,
        )
        _assert(calc_client.requests, f"{admin_user}: gateway-owned calculator should call upstream for synthesis")

        streaming_handler = FakeStreamingHandler()
        streaming_client = FakeClient([_chat_response(f"Streaming calculator synthesis for {admin_user}")])
        streaming_body = _chat_body(admin_user, workspaces[admin_user], "请用 streaming 计算 12 + 30")
        streaming_body["stream"] = True
        with patch("src.gateway_proxy.NativeProxyClient", return_value=streaming_client):
            run_streaming_orchestration(
                streaming_handler,
                "/v1/chat/completions",
                streaming_body,
            )
        _assert(streaming_client.requests, f"{admin_user}: streaming calculator should call upstream for synthesis")
        _assert("Streaming calculator synthesis" in "".join(streaming_handler.events), f"{admin_user}: streaming synthesis missing")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            token = base64.b64encode(b"admin:admin").decode("ascii")
            base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
            headers = {"authorization": f"Basic {token}"}

            runtime_qs = urllib.parse.urlencode({
                "limit": "20",
                "tenant_contains": admin_user,
                "workspace_contains": admin_workspace,
                "session_contains": f"pressure-session-{admin_user}",
                "has_rollup": "1",
            })
            runtime_req = urllib.request.Request(f"{base_url}/admin/agent-runtime.json?{runtime_qs}", headers=headers)
            with urllib.request.urlopen(runtime_req, timeout=5) as resp:
                runtime_payload = json.loads(resp.read().decode("utf-8"))
            runtime_text = json.dumps(runtime_payload, ensure_ascii=False)
            _assert(runtime_payload["runtime"]["agent_planner"]["session_count"] >= 1, "admin runtime missing planner sessions")
            _assert(runtime_payload["runtime"]["memory"]["rollup_count"] >= 1, "admin runtime missing rollups")
            _assert(f"ALPHA-{admin_user}" in runtime_text or f"BETA-{admin_user}" in runtime_text, "admin runtime missing own memory marker")
            for other in users[1:]:
                _assert(f"ALPHA-{other}" not in runtime_text and f"BETA-{other}" not in runtime_text, f"admin runtime leaked {other}")

            memories_qs = urllib.parse.urlencode({
                "limit": "20",
                "tenant_contains": admin_user,
                "workspace_contains": admin_workspace,
                "session_contains": f"pressure-session-{admin_user}",
                "has_rollup": "1",
            })
            memories_req = urllib.request.Request(f"{base_url}/admin/memories.json?{memories_qs}", headers=headers)
            with urllib.request.urlopen(memories_req, timeout=5) as resp:
                memories_payload = json.loads(resp.read().decode("utf-8"))
            memories_text = json.dumps(memories_payload, ensure_ascii=False)
            _assert(any(item.get("kind") == "session_rollup" for item in memories_payload.get("memories", [])), "admin memories missing rollup")
            for other in users[1:]:
                _assert(f"ALPHA-{other}" not in memories_text and f"BETA-{other}" not in memories_text, f"admin memories leaked {other}")

            events_qs = urllib.parse.urlencode({
                "limit": "20",
                "tenant_contains": admin_user,
                "workspace_contains": admin_workspace,
                "event_type": "memory_rollup",
            })
            events_req = urllib.request.Request(f"{base_url}/admin/agent-runtime-events.json?{events_qs}", headers=headers)
            with urllib.request.urlopen(events_req, timeout=5) as resp:
                events_payload = json.loads(resp.read().decode("utf-8"))
            _assert(any(event.get("event_type") == "memory_rollup" for event in events_payload.get("events", [])), "admin events missing memory_rollup")

            audit_qs = urllib.parse.urlencode({
                "limit": "100",
                "tenant_contains": admin_user,
                "workspace_contains": admin_workspace,
                "session_contains": f"pressure-session-{admin_user}",
            })
            audit_req = urllib.request.Request(f"{base_url}/admin/agent-runtime-audit.json?{audit_qs}", headers=headers)
            with urllib.request.urlopen(audit_req, timeout=5) as resp:
                audit_payload = json.loads(resp.read().decode("utf-8"))
            audit_text = json.dumps(audit_payload, ensure_ascii=False)
            requirements = audit_payload["audit"]["requirements"]
            required_keys = {
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
            _assert(set(requirements) == required_keys, "admin audit requirement keys drifted")
            for key in required_keys:
                _assert(requirements[key]["status"] == "proven/current_scope", f"admin audit did not prove {key}: {requirements[key]['status']}")
            _assert(not requirements["agent_planner_runtime_mode"]["detail"]["legacy_gateway_passthrough"], "admin audit thinks legacy gateway mode is active")
            _assert(not requirements["chat_only_upstream_config"]["detail"]["upstream_native_tool_authority"], "admin audit thinks upstream has native tool authority")
            _assert(not requirements["downstream_client_tool_execution_policy"]["detail"]["gateway_forces_local_user_side_tools"], "admin audit thinks Gateway executes user-side tools locally")
            _assert(audit_payload["audit"]["mode"] == "remote_agent_planner", "admin audit mode drifted")
            _assert(audit_payload["audit"]["overall_status"] == "proven/current_scope", "admin audit should be fully proven in pressure smoke")
            _assert(audit_payload["audit"]["summary"]["missing"] == 0, "admin audit has missing requirements")
            for other in users[1:]:
                _assert(f"ALPHA-{other}" not in audit_text and f"BETA-{other}" not in audit_text, f"admin audit leaked {other}")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

        print(json.dumps({
            "ok": True,
            "run_dir": str(run_root.resolve()),
            "users": len(users),
            "planner_sessions_checked": len(users),
            "memory_rollups_checked": len(users),
            "recall_payloads_checked": len(recall_payloads),
            "admin_runtime_checked": True,
            "admin_memories_checked": True,
            "admin_events_checked": True,
            "admin_audit_checked": True,
            "admin_audit_streaming_parity_checked": True,
        }, ensure_ascii=False, indent=2))
    finally:
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
        if planner is not None:
            planner._STORE = None


if __name__ == "__main__":
    main()
