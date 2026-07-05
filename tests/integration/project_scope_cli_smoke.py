#!/usr/bin/env python3
"""Self-contained project-scope smoke for Claude Code/Codex Gateway flows.

This verifies the important middle-layer invariant:
Gateway's service cwd/configured fallback root must not be treated as the
client project root.  Tool paths, project `.traces`, Skills/plugins, and
Memory must resolve against the downstream project root detected from the
request or CLI client metadata.

The script starts a temporary Gateway instance with an intentionally wrong
service root and an isolated downstream project root.  It does not require a
working upstream model because explicit Skill/Read/Memory requests are served
by the real local Gateway runtime.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]


def now_slug() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def post_json(base_url: str, key: str, path: str, payload: dict[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(base_url: str, path: str, *, timeout: float = 10.0) -> dict[str, Any]:
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def curl_sse(base_url: str, key: str, path: str, payload: dict[str, Any], out: pathlib.Path, *, timeout: float = 20.0) -> str:
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    out.write_text(text, encoding="utf-8")
    return text


def tool(base_url: str, key: str, name: str, arguments: dict[str, Any], workspace_root: pathlib.Path, out: pathlib.Path) -> dict[str, Any]:
    payload = {"workspace_root": str(workspace_root), "tool": name, "arguments": arguments}
    result = post_json(base_url, key, "/v1/tools/call", payload)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result.get("success"):
        raise AssertionError(f"{name} failed: {json.dumps(result, ensure_ascii=False)[:1000]}")
    return result


def content(result: dict[str, Any]) -> str:
    return str(result.get("content") or "")


class ChatOnlyUpstreamHandler(BaseHTTPRequestHandler):
    """Tiny chat-only upstream for real CLI two-turn tool smoke.

    It never emits native tools.  It only proves the gateway sends downstream
    tool results back into the chat-only upstream by returning a marker found in
    the raw request body.
    """

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        pass

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send_json({"object": "list", "data": [{"id": "mimo-v2.5-pro", "object": "model"}]})

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length", "0") or "0")).decode("utf-8", errors="replace")
        marker = "mock upstream ok"
        for candidate in ("CODEX-LIVE-SKILL-OK", "LIVE-SKILL-OK", "LIVE-TRACE-OK"):
            if candidate in raw:
                marker = candidate
                break
        path = self.path.split("?", 1)[0]
        if path.endswith("/messages"):
            self._send_json({
                "id": f"msg_mock_{hashlib.sha256(raw.encode()).hexdigest()[:12]}",
                "type": "message",
                "role": "assistant",
                "model": "mimo-v2.5-pro",
                "content": [{"type": "text", "text": marker}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
        elif path.endswith("/responses"):
            self._send_json({
                "id": f"resp_mock_{hashlib.sha256(raw.encode()).hexdigest()[:12]}",
                "object": "response",
                "status": "completed",
                "model": "mimo-v2.5-pro",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": marker}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
        else:
            self._send_json({
                "id": f"chatcmpl_mock_{hashlib.sha256(raw.encode()).hexdigest()[:12]}",
                "object": "chat.completion",
                "model": "mimo-v2.5-pro",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": marker}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })


def start_chat_only_upstream(run_dir: pathlib.Path) -> tuple[ThreadingHTTPServer, str]:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), ChatOnlyUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (run_dir / "upstream_port.txt").write_text(str(port), encoding="utf-8")
    return server, f"http://127.0.0.1:{port}"


def create_config(run_dir: pathlib.Path, port: int, key: str, service_root: pathlib.Path, upstream_base_url: str) -> pathlib.Path:
    cfg = {
        "admin": {
            "username": "admin",
            "password_hash": hashlib.sha256(b"admin").hexdigest(),
            "must_change_password": True,
        },
        "upstream": {
            "base_url": upstream_base_url,
            "api_key": "",
            "model": "mimo-v2.5-pro",
            "protocol": "openai_chat",
            "tools_enabled": "adapter",
            "native_tools_verified": False,
            "use_for_coding": True,
            "timeout_seconds": 5.0,
            "max_input_tokens": 1048576,
            "max_output_tokens": 131072,
            "max_concurrency": 8,
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
                "supports_parallel_tool_calls": False,
                "supports_vision": False,
                "supports_network": False,
                "supports_web_search": False,
                "supports_json_schema": True,
            },
            "name": "default",
            "id": "default",
        },
        "gateway": {
            "tool_mode": "orchestrate",
            "max_tool_rounds": 5,
            "workspace_root": str(service_root),
            "allow_write_tools": True,
            "allow_shell_tools": True,
            "request_logging": True,
            "logging_backend": "sqlite",
            "max_log_payload_chars": 200000,
            "sqlite_log_path": str((run_dir / "gateway.sqlite3").resolve()),
            "max_concurrent_requests": 8,
            "max_request_body_bytes": 67108864,
            "concurrency_queue_timeout_seconds": 5.0,
            "tool_execution_timeout_seconds": 30.0,
            "record_unsupported_tools": True,
            "text_tool_call_fallback_enabled": True,
            "text_tool_adapter_compact_token_limit": 48000,
            "intent_detection_enabled": True,
            "local_planner_enabled": True,
            "local_planner_max_files": 24,
            "local_planner_max_bytes_per_file": 24000,
            "public_base_url": f"http://127.0.0.1:{port}",
            "client_snippet_api_key": key,
            "downstream_model_alias": "mimo-v2.5-pro",
            "review_model_alias": "mimo-v2.5-pro",
            "codex_reasoning_effort": "none",
            "client_context_window": 1048576,
            "client_auto_compact_token_limit": 943718,
            "client_output_token_limit": 131072,
        },
        "context": {
            "enabled": True,
            "max_input_tokens": 1048576,
            "keep_recent_messages": 12,
            "summary_max_chars": 6000,
            "fanout_enabled": True,
            "fanout_chunk_tokens": 120000,
            "fanout_max_chunks": 0,
            "fanout_max_workers": 4,
            "quality_review_enabled": True,
            "memory_enabled": True,
            "memory_max_items": 200,
            "memory_recall_limit": 8,
            "memory_inject_max_chars": 4000,
            "memory_summary_max_chars": 900,
            "route_to_long_context": True,
            "long_context_upstream": {"base_url": "", "api_key": "", "model": "", "protocol": ""},
        },
        "downstream_keys": [
            {
                "name": "default",
                "key_hash": hashlib.sha256(key.encode()).hexdigest(),
                "prefix": key[:8],
                "enabled": True,
                "protocols": ["models", "chat_completions", "responses", "messages", "direct_tools"],
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        ],
        "mcp": {"servers": [], "marketplace_enabled": True},
        "http_actions": {"enabled": True, "actions": []},
        "upstream_profiles": [],
    }
    path = run_dir / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def create_fixture(run_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    project_root = (run_dir / "downstream-project").resolve()
    service_root = (run_dir / "gateway-service-root").resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    service_root.mkdir(parents=True, exist_ok=True)

    write(project_root / ".traces/2026-05-25/trace.txt", "live-trace-marker: LIVE-TRACE-OK\n")
    write(service_root / ".traces/2026-05-25/trace.txt", "live-trace-marker: SERVICE-TRACE-WRONG\n")

    write(
        project_root / ".claude/skills/live-skill/SKILL.md",
        "---\nname: live-skill\ndescription: live project scoped Claude skill smoke\n---\n# live-skill\n\nLIVE-SKILL-OK\n",
    )
    write(
        service_root / ".claude/skills/live-skill/SKILL.md",
        "---\nname: live-skill\ndescription: wrong service skill\n---\n# live-skill\n\nSERVICE-SKILL-WRONG\n",
    )
    write(
        project_root / ".codex/skills/codex-live-skill/SKILL.md",
        "---\nname: codex-live-skill\ndescription: live project scoped Codex skill smoke\n---\n# codex-live-skill\n\nCODEX-LIVE-SKILL-OK\n",
    )
    write(
        service_root / ".codex/skills/codex-live-skill/SKILL.md",
        "---\nname: codex-live-skill\ndescription: wrong service codex skill\n---\n# codex-live-skill\n\nSERVICE-SKILL-WRONG\n",
    )
    write(project_root / ".codex/plugins/demo/.codex-plugin/plugin.json", '{"name":"demo","skills":"./skills"}\n')
    write(
        project_root / ".codex/plugins/demo/skills/plugin-live-skill/SKILL.md",
        "---\nname: plugin-live-skill\ndescription: live project plugin skill smoke\n---\n# plugin-live-skill\n\nPLUGIN-LIVE-SKILL-OK\n",
    )
    subprocess.run(["git", "init", "-q"], cwd=project_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return project_root, service_root


def start_gateway(config_path: pathlib.Path, port: int, run_dir: pathlib.Path) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["GATEWAY_CONFIG_PATH"] = str(config_path)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "src/toolcall_gateway.py"), "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=(run_dir / "server.log").open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )
    (run_dir / "server.pid").write_text(str(proc.pid), encoding="utf-8")
    return proc


def wait_health(base_url: str, proc: subprocess.Popen[str], timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"gateway exited early: rc={proc.returncode}")
        try:
            health = get_json(base_url, "/healthz", timeout=1.0)
            if health.get("ok"):
                return health
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.1)
    raise TimeoutError(f"gateway did not become healthy: {last_error}")


def run_claude(base_url: str, key: str, project_root: pathlib.Path, run_dir: pathlib.Path, *, require: bool) -> tuple[bool, dict[str, Any]]:
    claude = shutil.which("claude")
    if not claude:
        return (not require), {"checked": False, "skipped": True, "reason": "claude binary not found"}
    env = os.environ.copy()
    env.update(
        {
            "ANTHROPIC_BASE_URL": base_url.rstrip("/") + "/anthropic",
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CONFIG_DIR": str((run_dir / "claude_config").resolve()),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    pathlib.Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
    out_path = run_dir / "claude.out"
    err_path = run_dir / "claude.err"
    with out_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
        proc = subprocess.run(
            [claude, "-p", "--output-format", "text", "--dangerously-skip-permissions", "Read skill live-skill and reply only with LIVE-SKILL-OK."],
            cwd=project_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            timeout=60,
            text=True,
        )
    output = out_path.read_text(encoding="utf-8", errors="replace")
    ok = proc.returncode == 0 and "LIVE-SKILL-OK" in output and "SERVICE-SKILL-WRONG" not in output
    return ok, {"checked": True, "rc": proc.returncode, "ok": ok}


def run_codex(base_url: str, key: str, project_root: pathlib.Path, run_dir: pathlib.Path, *, require: bool) -> tuple[bool, dict[str, Any]]:
    codex = shutil.which("codex")
    if not codex:
        return (not require), {"checked": False, "skipped": True, "reason": "codex binary not found"}
    codex_home = (run_dir / "codex_home").resolve()
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                'model_provider = "gateway"',
                'model = "mimo-v2.5-pro"',
                'model_reasoning_effort = "none"',
                'model_context_window = 1048576',
                'model_max_output_tokens = 131072',
                '[model_providers.gateway]',
                'name = "gateway"',
                f'base_url = "{base_url.rstrip("/")}/v1"',
                'env_key = "OPENAI_API_KEY"',
                'wire_api = "responses"',
                f'[projects."{project_root}"]',
                'trust_level = "trusted"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({"CODEX_HOME": str(codex_home), "OPENAI_API_KEY": key, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    out_path = run_dir / "codex.out"
    err_path = run_dir / "codex.err"
    with out_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
        proc = subprocess.run(
            [
                codex,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "danger-full-access",
                "-C",
                str(project_root),
                f"Read {project_root / '.codex/skills/codex-live-skill/SKILL.md'} and reply only with CODEX-LIVE-SKILL-OK.",
            ],
            cwd=project_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            timeout=60,
            text=True,
        )
    output = out_path.read_text(encoding="utf-8", errors="replace")
    ok = proc.returncode == 0 and "CODEX-LIVE-SKILL-OK" in output and "SERVICE-SKILL-WRONG" not in output
    return ok, {"checked": True, "rc": proc.returncode, "ok": ok}


def main() -> int:
    # Keep the smoke self-contained even on developer machines with a global
    # HTTP(S) proxy.  urllib honors proxy env vars in the parent process; without
    # this, 127.0.0.1 health/tool calls can be sent to a local proxy and fail
    # before the Gateway is actually exercised.
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", default="", help="Directory for smoke artifacts; defaults under .gateway_runtime")
    parser.add_argument("--require-claude", action="store_true", help="Fail when Claude CLI is missing or fails")
    parser.add_argument("--require-codex", action="store_true", help="Fail when Codex CLI is missing or fails")
    parser.add_argument("--skip-claude", action="store_true", help="Skip Claude CLI even if installed")
    parser.add_argument("--skip-codex", action="store_true", help="Skip Codex CLI even if installed")
    args = parser.parse_args()

    run_dir = pathlib.Path(args.artifact_dir) if args.artifact_dir else ROOT / ".gateway_runtime" / f"project-scope-cli-smoke-{now_slug()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    project_root, service_root = create_fixture(run_dir)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    key = "project-smoke-key-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:12]
    upstream_server, upstream_base_url = start_chat_only_upstream(run_dir)
    config_path = create_config(run_dir, port, key, service_root, upstream_base_url)

    proc = start_gateway(config_path, port, run_dir)
    summary: dict[str, Any] = {
        "pass": False,
        "run_dir": str(run_dir),
        "port": port,
        "project_root": str(project_root),
        "service_root": str(service_root),
    }
    try:
        health = wait_health(base_url, proc)
        (run_dir / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["health_ok"] = health.get("ok") is True
        summary["builtin_tool_count"] = health.get("builtin_tool_count")

        list_skills = tool(base_url, key, "Skill", {}, project_root, run_dir / "direct_list_skills.json")
        read_skill = tool(base_url, key, "Skill", {"name": "live-skill"}, project_root, run_dir / "direct_read_skill.json")
        read_plugin = tool(base_url, key, "Skill", {"name": "plugin-live-skill"}, project_root, run_dir / "direct_read_plugin_skill.json")
        read_trace = tool(base_url, key, "Read", {"file_path": ".traces/2026-05-25/trace.txt"}, project_root, run_dir / "direct_read_trace.json")
        read_trace_abs = tool(base_url, key, "Read", {"file_path": str(project_root / ".traces/2026-05-25/trace.txt")}, project_root, run_dir / "direct_read_trace_absolute.json")
        function_call_result = post_json(
            base_url,
            key,
            "/v1/functions/call",
            {
                "workspace_root": str(project_root),
                "function": {"name": "Read", "arguments": json.dumps({"file_path": ".traces/2026-05-25/trace.txt"})},
                "call_id": "project_scope_function_call",
            },
        )
        (run_dir / "direct_function_call_trace.json").write_text(json.dumps(function_call_result, ensure_ascii=False, indent=2), encoding="utf-8")

        list_content = content(list_skills)
        summary["direct_skills_ok"] = "LIVE-SKILL-OK" in content(read_skill) and "SERVICE-SKILL-WRONG" not in content(read_skill)
        summary["direct_plugin_skill_ok"] = "PLUGIN-LIVE-SKILL-OK" in content(read_plugin)
        summary["direct_trace_ok"] = "LIVE-TRACE-OK" in content(read_trace) and "SERVICE-TRACE-WRONG" not in content(read_trace)
        summary["direct_trace_absolute_ok"] = "LIVE-TRACE-OK" in content(read_trace_abs) and "SERVICE-TRACE-WRONG" not in content(read_trace_abs)
        summary["direct_function_call_trace_ok"] = bool(function_call_result.get("success")) and "LIVE-TRACE-OK" in content(function_call_result) and "SERVICE-TRACE-WRONG" not in content(function_call_result)
        summary["direct_list_has_project_skills"] = all(name in list_content for name in ("live-skill", "codex-live-skill", "plugin-live-skill")) and str(project_root) in list_content
        summary["direct_list_leaks_service_skills"] = "SERVICE-SKILL-WRONG" in list_content or str(service_root / ".claude/skills/live-skill") in list_content or str(service_root / ".codex/skills/codex-live-skill") in list_content

        tool(base_url, key, "SaveMemory", {"action": "write", "summary": "SERVICE-MEMORY-WRONG", "keywords": ["service-memory-wrong"], "session_key": "service-scope"}, service_root, run_dir / "direct_service_save_memory.json")
        tool(base_url, key, "SaveMemory", {"action": "write", "summary": "LIVE-MEMORY-OK project scoped", "keywords": ["live-memory-ok"], "session_key": "project-scope"}, project_root, run_dir / "direct_project_save_memory.json")
        memories_result = tool(base_url, key, "RecallMemory", {"action": "list", "limit": 10}, project_root, run_dir / "direct_recall_memory.json")
        memories = json.loads(content(memories_result)).get("memories", [])
        summary["memory_project_root_ok"] = any(m.get("workspace_root") == str(project_root) and "LIVE-MEMORY-OK" in m.get("summary", "") for m in memories)
        summary["memory_service_root_leak"] = any(m.get("workspace_root") == str(service_root) or "SERVICE-MEMORY-WRONG" in m.get("summary", "") for m in memories)

        anthropic_sse = curl_sse(
            base_url,
            key,
            "/anthropic/v1/messages",
            {
                "model": "mimo-v2.5-pro",
                "max_tokens": 200,
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Old compacted note **Worktree:** {service_root}\nPrimary working directory: {project_root}\nRead skill live-skill and reply only with LIVE-SKILL-OK.",
                    }
                ],
            },
            run_dir / "anthropic_skill.sse",
        )
        responses_sse = curl_sse(
            base_url,
            key,
            "/v1/responses",
            {
                "model": "mimo-v2.5-pro",
                "stream": True,
                "input": f"<environment_context>\n  <cwd>{project_root}</cwd>\n</environment_context>\nRead skill codex-live-skill and reply only with CODEX-LIVE-SKILL-OK.",
            },
            run_dir / "responses_skill.sse",
        )
        summary["anthropic_stream_skill_ok"] = (
            ("LIVE-SKILL-OK" in anthropic_sse or ('"type": "tool_use"' in anthropic_sse and '"name": "Skill"' in anthropic_sse))
            and "SERVICE-SKILL-WRONG" not in anthropic_sse
        )
        summary["responses_stream_skill_ok"] = (
            ("CODEX-LIVE-SKILL-OK" in responses_sse or '"type": "function_call"' in responses_sse)
            and "SERVICE-SKILL-WRONG" not in responses_sse
        )
        summary["responses_stream_order_ok"] = "response.created" in responses_sse and "response.completed" in responses_sse and responses_sse.find("response.created") < responses_sse.find("response.completed")

        if args.skip_claude:
            claude_ok, claude_info = True, {"checked": False, "skipped": True, "reason": "--skip-claude"}
        else:
            claude_ok, claude_info = run_claude(base_url, key, project_root, run_dir, require=args.require_claude)
        if args.skip_codex:
            codex_ok, codex_info = True, {"checked": False, "skipped": True, "reason": "--skip-codex"}
        else:
            codex_ok, codex_info = run_codex(base_url, key, project_root, run_dir, require=args.require_codex)
        summary["claude"] = claude_info
        summary["codex"] = codex_info
        summary["claude_skill_ok"] = bool(claude_ok)
        summary["codex_skill_ok"] = bool(codex_ok)

        required = [
            "health_ok",
            "direct_skills_ok",
            "direct_plugin_skill_ok",
            "direct_trace_ok",
            "direct_trace_absolute_ok",
            "direct_function_call_trace_ok",
            "direct_list_has_project_skills",
            "memory_project_root_ok",
            "anthropic_stream_skill_ok",
            "responses_stream_skill_ok",
            "responses_stream_order_ok",
            "claude_skill_ok",
            "codex_skill_ok",
        ]
        summary["pass"] = all(bool(summary.get(key_name)) for key_name in required) and not summary.get("direct_list_leaks_service_skills") and not summary.get("memory_service_root_leak")
        return 0 if summary["pass"] else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        upstream_server.shutdown()
        upstream_server.server_close()
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
