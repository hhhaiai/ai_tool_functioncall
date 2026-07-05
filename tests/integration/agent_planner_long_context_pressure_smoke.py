#!/usr/bin/env python3
"""Long-context remote Agent Planner pressure smoke.

This proves the remote Agent Runtime can keep per-tenant conversation memory when
an upstream chat-only model has a tiny context window: large turns are rolled up,
streaming /v1/responses recall is injected before the upstream request, transport
payloads are compacted, and one client never receives another client's markers.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import sys
import threading
import time
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
        self.lock = threading.Lock()

    def forward(self, path: str, body: Json) -> Json:
        with self.lock:
            self.requests.append((path, body))
            if self.responses:
                return self.responses.pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}


class FakeStreamHandler:
    def __init__(self):
        self.events: list[str] = []

    class _WFile:
        def __init__(self, owner: "FakeStreamHandler"):
            self.owner = owner

        def write(self, data):
            self.owner.events.append(data.decode("utf-8") if isinstance(data, bytes) else str(data))

        def flush(self):
            pass

    @property
    def wfile(self):
        return self._WFile(self)

    def send_response(self, status):
        self.events.append(f"STATUS:{status}\n")

    def send_header(self, key, value):
        self.events.append(f"HEADER:{key}:{value}\n")

    def end_headers(self):
        self.events.append("END_HEADERS\n")


def _chat_response(text: str) -> Json:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _metadata(user: str) -> Json:
    return {"session_id": f"longctx-session-{user}", "user_id": json.dumps({"user_id": user})}


def _chat_body(user: str, root: pathlib.Path, text: str) -> Json:
    return {
        "model": "weak",
        # Remote clients must send an explicit client workspace path; relative
        # harness paths must not be resolved against the Gateway service cwd.
        "workspace_root": str(root.resolve()),
        "metadata": _metadata(user),
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 128,
    }


def _responses_stream_body(user: str, root: pathlib.Path, text: str) -> Json:
    return {
        "model": "weak",
        "stream": True,
        "workspace_root": str(root.resolve()),
        "metadata": _metadata(user),
        "input": text,
        "max_output_tokens": 128,
    }


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    run_root = pathlib.Path(".gateway_runtime") / f"agent-planner-long-context-pressure-{time.strftime('%Y%m%d-%H%M%S')}"
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
        cfg["gateway"]["local_planner_enabled"] = False
        cfg["context"]["enabled"] = True
        cfg["context"]["max_input_tokens"] = 400
        cfg["context"]["keep_recent_messages"] = 3
        cfg["context"]["summary_max_chars"] = 1200
        cfg["context"]["memory_enabled"] = True
        cfg["context"]["memory_rollup_every_turns"] = 2
        cfg["context"]["memory_rollup_max_chars"] = 2200
        cfg["context"]["memory_inject_max_chars"] = 5000
        cfg["context"]["memory_summary_max_chars"] = 1400
        cfg["upstream"]["protocol"] = "openai_chat"
        cfg["upstream"]["tools_enabled"] = "adapter"
        cfg["upstream"]["capabilities"]["supports_tools"] = False
        cfg["upstream"]["capabilities"]["supports_function_calls"] = False
        gateway.save_config(cfg)

        users = [f"longctx-user-{idx}" for idx in range(4)]
        workspaces: dict[str, pathlib.Path] = {}
        markers = {user: f"LONGCTX-CANARY-{user}" for user in users}
        for user in users:
            root = run_root / "clients" / user
            root.mkdir(parents=True)
            (root / "README.md").write_text(f"workspace for {user}\n", encoding="utf-8")
            workspaces[user] = root

        def remember_worker(user: str) -> None:
            root = workspaces[user]
            marker = markers[user]
            for turn in range(3):
                huge = (
                    f"Remember this remote long-context marker near the front: {marker}; turn={turn}.\n"
                    + (f"bulk-{user}-{turn}-" * 900)
                    + f"\nEND-BULK-{user}-{turn}"
                )
                client = FakeClient([_chat_response(f"Stored {marker} turn {turn}")])
                run_tool_orchestration("/v1/chat/completions", _chat_body(user, root, huge), client)
                _assert(client.requests, f"{user}: upstream chat request missing on remember turn {turn}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(users)) as executor:
            list(executor.map(remember_worker, users))

        stream_checks: dict[str, Json] = {}
        for user in users:
            root = workspaces[user]
            marker = markers[user]
            oversized_current_input = (
                f"What remote long-context marker did we record for {user}? Use recalled memory.\n"
                + ("CURRENT-HUGE-FILLER-" * 1200)
                + f"\nCURRENT-FILLER-END-{user}"
            )
            client = FakeClient([_chat_response(f"Streaming recall complete for {user}: {marker}")])
            handler = FakeStreamHandler()
            with patch("src.gateway_proxy.NativeProxyClient", return_value=client):
                run_streaming_orchestration(
                    handler,
                    "/v1/responses",
                    _responses_stream_body(user, root, oversized_current_input),
                )
            _assert(client.requests, f"{user}: streaming recall did not call upstream")
            upstream_path, upstream_body = client.requests[0]
            payload = json.dumps(upstream_body, ensure_ascii=False)
            stream_text = "".join(handler.events)
            _assert(upstream_path == "/v1/chat/completions", f"{user}: unexpected upstream path {upstream_path}")
            _assert("Gateway recalled memory" in payload, f"{user}: recalled memory not injected")
            _assert(marker in payload, f"{user}: own marker not recalled")
            _assert('"gateway_context"' not in payload, f"{user}: internal gateway_context leaked to upstream payload")
            _assert(
                '"compacted": true' in stream_text or '"compacted":true' in stream_text,
                f"{user}: streaming response did not expose compacted context metadata",
            )
            _assert(payload.count("CURRENT-HUGE-FILLER-") < 1200, f"{user}: oversized filler was not reduced")
            _assert(len(payload) < len(oversized_current_input), f"{user}: upstream payload was not smaller than raw input")
            _assert(f"Streaming recall complete for {user}" in stream_text, f"{user}: final SSE text missing")
            _assert("event: error" not in stream_text, f"{user}: streaming returned error")
            for other in users:
                if other != user:
                    _assert(markers[other] not in payload, f"{user}: leaked recalled marker from {other}")
            stream_checks[user] = {
                "upstream_payload_chars": len(payload),
                "sse_chars": len(stream_text),
                "compacted": True,
            }

        for user, root in workspaces.items():
            workspace_key = str(root.resolve())
            memories = gateway._sqlite_tail_memories(
                50,
                tenant_contains=user,
                workspace_contains=workspace_key,
                session_contains=f"longctx-session-{user}",
            )
            _assert(any(mem.get("kind") == "session_rollup" for mem in memories), f"{user}: missing session_rollup")
            memory_text = json.dumps(memories, ensure_ascii=False)
            _assert(markers[user] in memory_text, f"{user}: own marker missing from memories")
            for other in users:
                if other != user:
                    _assert(markers[other] not in memory_text, f"{user}: memory DB leaked {other}")
            events = planner.list_runtime_events(50, tenant_contains=user, workspace_contains=workspace_key, event_type="memory_rollup")
            _assert(events, f"{user}: missing memory_rollup runtime event")

        print(json.dumps({
            "ok": True,
            "run_dir": str(run_root.resolve()),
            "users": len(users),
            "rollups_checked": len(users),
            "streaming_responses_recall_checked": len(stream_checks),
            "compaction_checked": True,
            "cross_tenant_leak_checked": True,
            "stream_checks": stream_checks,
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
