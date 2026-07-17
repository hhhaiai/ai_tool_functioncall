#!/usr/bin/env python3
"""MCP (Model Context Protocol) server management.

Handles MCP server connections, tool discovery, and tool execution.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import select
import subprocess
import threading
import time
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .gateway_builtin_tools import GatewayTool

from .gateway_errors import ToolExecutionError
from .gateway_process_ops import (
    BoundedProcessStream,
    process_group_kwargs,
    terminate_process_group,
)
from .gateway_sandbox import (
    SANDBOX_WORKER_ERROR_PREFIX,
    SANDBOX_WORKER_SETUP_EXIT,
    sandbox_child_environment,
    sandbox_worker_command,
    workspace_job,
)

Json = dict[str, Any]

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CATALOG_CACHE_TTL_SECONDS = 60
MCP_SESSIONS: dict[str, "McpSession"] = {}
MCP_SESSIONS_LOCK = threading.Lock()
MCP_TOOL_CATALOG_CACHE: dict[str, tuple[float, list[Json]]] = {}
MCP_SERVER_STATUS: dict[str, Json] = {}


class McpSession:
    def __init__(self, server: Json) -> None:
        self.server = copy.deepcopy(server)
        self.name = str(server.get("name") or "")
        self.timeout = float(server.get("timeout") or os.environ.get("GATEWAY_MCP_TIMEOUT", "20"))
        self.proc = _mcp_start(server)
        self.stderr_capture = BoundedProcessStream(_mcp_stderr_limit(server))
        self.stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self.stderr_thread.start()
        self.lock = threading.Lock()
        self.next_id = 1
        self.last_used_at = time.time()
        try:
            _mcp_initialize(self.proc, server, self.timeout, self._next_id_locked())
        except Exception as exc:
            self.close()
            if isinstance(exc, ToolExecutionError):
                raise
            stderr = self.stderr_text().strip()
            failure_type = (
                "sandbox_setup_failed"
                if self.proc.returncode == SANDBOX_WORKER_SETUP_EXIT
                and stderr.lstrip().startswith(SANDBOX_WORKER_ERROR_PREFIX)
                else "execution_failed"
            )
            detail = stderr if failure_type == "sandbox_setup_failed" else f"{exc.__class__.__name__}: {exc}"
            raise ToolExecutionError(
                f"MCP server {self.name or '<unnamed>'} initialization failed: {detail}",
                failure_type=failure_type,
            ) from exc

    def _next_id_locked(self) -> int:
        request_id = self.next_id
        self.next_id += 1
        return request_id

    def request(self, method: str, params: Json | None = None) -> Json:
        with self.lock:
            if self.proc.poll() is not None:
                raise ToolExecutionError(f"MCP server {self.name} exited", failure_type="execution_failed")
            self.last_used_at = time.time()
            try:
                return _mcp_request(
                    self.proc,
                    method,
                    params,
                    request_id=self._next_id_locked(),
                    timeout=self.timeout,
                )
            except Exception as exc:
                # A timeout/framing failure leaves stdio synchronization
                # unknown. Never reuse that process for another request.
                self.close()
                if isinstance(exc, ToolExecutionError):
                    raise
                raise ToolExecutionError(
                    f"MCP server {self.name or '<unnamed>'} request failed: {exc.__class__.__name__}: {exc}",
                    failure_type="execution_failed",
                ) from exc

    def _drain_stderr(self) -> None:
        pipe = self.proc.stderr
        if pipe is None:
            return
        try:
            while True:
                chunk = pipe.read(65_536)
                if not chunk:
                    break
                self.stderr_capture.feed(chunk)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def stderr_text(self) -> str:
        return self.stderr_capture.text()

    def close(self) -> None:
        proc = self.proc
        try:
            terminate_process_group(proc, timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if hasattr(self, "stderr_thread"):
            self.stderr_thread.join(timeout=2)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass


def _mcp_limits() -> Json:
    try:
        from .gateway_config import _gateway_config
        cfg = _gateway_config().get("mcp") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _positive_limit(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value or default))
    except (TypeError, ValueError, OverflowError):
        return default


def _mcp_header_limit(server: Json | None = None) -> int:
    cfg = _mcp_limits()
    value = (server or {}).get("max_header_bytes", cfg.get("max_header_bytes"))
    return _positive_limit(value, 64 * 1024, minimum=1024)


def _mcp_message_limit(server: Json | None = None) -> int:
    cfg = _mcp_limits()
    value = (server or {}).get("max_message_bytes", cfg.get("max_message_bytes"))
    return _positive_limit(value, 16 * 1024 * 1024, minimum=1024)


def _mcp_stderr_limit(server: Json | None = None) -> int:
    cfg = _mcp_limits()
    value = (server or {}).get("max_stderr_bytes", cfg.get("max_stderr_bytes"))
    return _positive_limit(value, 256 * 1024, minimum=1024)


def _mcp_safe_component(value: str, *, default: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-._")
    return cleaned or default


def _mcp_public_name(server_name: str, tool_name: str) -> str:
    safe_server = _mcp_safe_component(server_name, default="server")
    safe_tool = _mcp_safe_component(tool_name, default="tool")
    return f"mcp__{safe_server}__{safe_tool}"


def _mcp_legacy_public_name(server_name: str, tool_name: str) -> str:
    return f"mcp_{server_name}_{tool_name}"


def _mcp_parse_public_name(name: str) -> tuple[str, str] | None:
    # Only parse the unambiguous mcp__server__tool format. The older
    # mcp_server_tool schema name is still exposed for client compatibility but
    # is not parsed here because server/tool boundaries are ambiguous.
    if name.startswith("mcp__"):
        parts = name[5:].split("__", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
    return None


def _enabled_mcp_servers() -> list[Json]:
    from .gateway_config import load_config
    cfg = load_config()
    mcp_cfg = cfg.get("mcp", {})
    servers = mcp_cfg.get("servers") or []
    return [s for s in servers if isinstance(s, dict) and s.get("enabled", True)]


def _mcp_server_by_name(name: str) -> Json | None:
    for server in _enabled_mcp_servers():
        if server.get("name") == name:
            return server
    return None


def _mcp_env(server: Json) -> dict[str, str]:
    server_env = server.get("env") or {}
    return sandbox_child_environment(server_env if isinstance(server_env, dict) else None)


def _mcp_command(server: Json) -> list[str]:
    command = server.get("command")
    if isinstance(command, str):
        import shlex
        parts = shlex.split(command)
    elif isinstance(command, list):
        parts = [str(c) for c in command]
    else:
        raise ToolExecutionError("MCP server missing 'command'", failure_type="invalid_input")
    args = server.get("args") or []
    if isinstance(args, str):
        import shlex
        parts.extend(shlex.split(args))
    elif isinstance(args, list):
        parts.extend(str(arg) for arg in args)
    return parts


_MCP_SERVICE_FILE_ARGUMENT_KEYS = {
    "cwd",
    "dbpath",
    "destination",
    "dir",
    "directory",
    "dst",
    "file",
    "filepath",
    "folder",
    "inputpath",
    "outputpath",
    "path",
    "repo",
    "repopath",
    "repository",
    "resourceuri",
    "root",
    "source",
    "src",
    "target",
    "uri",
    "workdir",
    "workingdir",
}


def _mcp_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _mcp_tool_config(server: Json, tool_name: str | None) -> Json:
    if not tool_name:
        return {}
    tools = server.get("tools")
    if isinstance(tools, dict):
        item = tools.get(tool_name) or tools.get(_mcp_safe_component(tool_name, default="tool"))
        if isinstance(item, dict):
            return item
    if isinstance(tools, list):
        for item in tools:
            if isinstance(item, dict) and item.get("name") == tool_name:
                return item
    return {}


def _mcp_allows_service_file_arguments(server: Json, tool_name: str | None = None) -> bool:
    tool_cfg = _mcp_tool_config(server, tool_name)
    for cfg in (tool_cfg, server):
        if _mcp_bool(cfg.get("allow_service_file_arguments")):
            return True
        if _mcp_bool(cfg.get("allow_service_files")):
            return True
        if _mcp_bool(cfg.get("allow_file_arguments")):
            return True
    allowed_tools = server.get("service_file_argument_tools") or server.get("allow_service_file_tools")
    if isinstance(allowed_tools, list) and tool_name in {str(item) for item in allowed_tools}:
        return True
    return False


def _mcp_argument_key_is_file_target(key: str) -> bool:
    cleaned = re.sub(r"[^a-z0-9]+", "", key.lower())
    return cleaned in _MCP_SERVICE_FILE_ARGUMENT_KEYS


def _mcp_value_looks_like_service_file_target(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("file://"):
        return True
    if lowered.startswith(("http://", "https://", "mailto:", "urn:")):
        return False
    if text in {".", ".."}:
        return True
    if text.startswith(("/", "\\", "~/", "~\\", "../", "..\\", "./", ".\\")):
        return True
    if re.match(r"^[a-zA-Z]:[\\/]", text):
        return True
    if "/" in text or "\\" in text:
        return True
    if re.search(r"\.[A-Za-z0-9]{1,12}$", text) and not re.search(r"\s", text):
        return True
    return False


def _mcp_find_service_file_argument(value: Any, *, key: str | None = None, inherited_file_key: bool = False) -> str | None:
    is_file_key = inherited_file_key or (key is not None and _mcp_argument_key_is_file_target(key))
    if isinstance(value, str):
        if is_file_key and _mcp_value_looks_like_service_file_target(value):
            return key or "argument"
        if value.strip().lower().startswith("file://"):
            return key or "argument"
        return None
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            hit = _mcp_find_service_file_argument(
                child_value,
                key=str(child_key),
                inherited_file_key=is_file_key,
            )
            if hit:
                return hit
    elif isinstance(value, list):
        for item in value:
            hit = _mcp_find_service_file_argument(item, key=key, inherited_file_key=is_file_key)
            if hit:
                return hit
    return None


def _mcp_validate_service_file_arguments(server: Json, arguments: Json, *, tool_name: str | None = None) -> None:
    if _mcp_allows_service_file_arguments(server, tool_name):
        return
    hit = _mcp_find_service_file_argument(arguments)
    if hit:
        raise ToolExecutionError(
            f"MCP argument '{hit}' looks like a Gateway service filesystem target; "
            "pass user workspace paths to downstream client tools, or set "
            "allow_service_file_arguments=true on the MCP server/tool for an admin-approved service endpoint",
            failure_type="invalid_input",
        )


def _mcp_write_message(proc: subprocess.Popen, message: Json) -> None:
    data = json.dumps(message, ensure_ascii=False)
    encoded = data.encode()
    max_message_bytes = _mcp_message_limit()
    if len(encoded) > max_message_bytes:
        raise ToolExecutionError(
            f"MCP request exceeds message limit ({len(encoded)} > {max_message_bytes} bytes)",
            failure_type="invalid_input",
        )
    content = f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded
    proc.stdin.write(content)
    proc.stdin.flush()


def _mcp_read_exact(stream: Any, length: int, timeout: float) -> bytes:
    deadline = time.time() + timeout
    buf = bytearray()
    fd = stream.fileno()
    while len(buf) < length:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("MCP read timeout")
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.1))
        if ready:
            chunk = os.read(fd, length - len(buf))
            if not chunk:
                raise EOFError("MCP stream closed")
            buf.extend(chunk)
    return bytes(buf)


def _mcp_read_message(proc: subprocess.Popen, timeout: float) -> Json:
    deadline = time.time() + timeout
    header = b""
    max_header_bytes = _mcp_header_limit()
    max_message_bytes = _mcp_message_limit()
    stdout_fd = proc.stdout.fileno()
    while b"\r\n\r\n" not in header:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("MCP header read timeout")
        ready, _, _ = select.select([stdout_fd], [], [], min(remaining, 0.1))
        if ready:
            byte = os.read(stdout_fd, 1)
            if not byte:
                raise EOFError("MCP stream closed")
            header += byte
            if len(header) > max_header_bytes:
                raise ToolExecutionError(
                    f"MCP response header exceeds limit ({max_header_bytes} bytes)",
                    failure_type="execution_failed",
                )
    header_str = header.decode("utf-8", errors="replace")
    content_length = 0
    for line in header_str.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
    if content_length == 0:
        raise ValueError("Missing Content-Length header")
    if content_length < 0:
        raise ValueError("Invalid negative Content-Length header")
    if content_length > max_message_bytes:
        raise ToolExecutionError(
            f"MCP response exceeds message limit ({content_length} > {max_message_bytes} bytes)",
            failure_type="execution_failed",
        )
    body = _mcp_read_exact(proc.stdout, content_length, deadline - time.time())
    return json.loads(body.decode("utf-8"))


def _mcp_request(proc: subprocess.Popen, method: str, params: Json | None = None, *, request_id: int = 1, timeout: float = 20) -> Json:
    message: Json = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        message["params"] = params
    _mcp_write_message(proc, message)
    response = _mcp_read_message(proc, timeout)
    if "error" in response:
        error = response["error"]
        raise ToolExecutionError(
            f"MCP error: {error.get('message', 'unknown')}",
            failure_type="execution_failed",
        )
    return response.get("result") or {}


def _mcp_notify(proc: subprocess.Popen, method: str, params: Json | None = None) -> None:
    message: Json = {"jsonrpc": "2.0", "method": method}
    if params:
        message["params"] = params
    _mcp_write_message(proc, message)


def _mcp_start(server: Json) -> subprocess.Popen:
    command = _mcp_command(server)
    env = _mcp_env(server)
    cwd = pathlib.Path(str(server.get("cwd") or os.getcwd())).resolve()
    job = workspace_job(
        command,
        cwd,
        shell=False,
        timeout_seconds=float(server.get("timeout") or os.environ.get("GATEWAY_MCP_TIMEOUT", "20")),
        max_output_bytes=_mcp_message_limit(server),
        writable_paths=tuple(str(path) for path in (server.get("writable_paths") or (".",))),
        long_lived=True,
        network_policy=str(server.get("network_policy") or "inherited"),
    )
    return subprocess.Popen(
        sandbox_worker_command(job),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(cwd),
        bufsize=0,
        **process_group_kwargs(),
    )


def _mcp_initialize(proc: subprocess.Popen, server: Json, timeout: float, request_id: int = 1) -> None:
    _mcp_request(
        proc,
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "gateway", "version": "1.0"},
        },
        request_id=request_id,
        timeout=timeout,
    )
    _mcp_notify(proc, "notifications/initialized")


def _mcp_with_server(server: Json, fn: Callable[[subprocess.Popen, float], Any]) -> Any:
    session = _mcp_get_session(server)
    return fn(session.proc, session.timeout)


def _mcp_session_key(server: Json) -> str:
    name = str(server.get("name") or "")
    command = " ".join(_mcp_command(server))
    cwd = str(server.get("cwd") or "")
    return f"{name}:{cwd}:{command}"


def _mcp_use_pool(server: Json) -> bool:
    return server.get("pool", True)


def _mcp_get_session(server: Json) -> McpSession:
    key = _mcp_session_key(server)
    with MCP_SESSIONS_LOCK:
        session = MCP_SESSIONS.get(key)
        if session and session.proc.poll() is None:
            return session
        if session:
            session.close()
        try:
            session = McpSession(server)
        except Exception as exc:
            server_name = str(server.get("name") or key)
            _mcp_set_status(server_name, "broken", detail=str(exc), tool_count=0)
            MCP_SESSIONS.pop(key, None)
            MCP_TOOL_CATALOG_CACHE.pop(key, None)
            raise
        MCP_SESSIONS[key] = session
        return session


def _mcp_close_sessions() -> None:
    with MCP_SESSIONS_LOCK:
        for session in MCP_SESSIONS.values():
            session.close()
        MCP_SESSIONS.clear()


def _mcp_catalog_ttl(server: Json) -> float:
    return float(server.get("catalog_ttl") or MCP_CATALOG_CACHE_TTL_SECONDS)


def _mcp_cache_key(server: Json) -> str:
    return _mcp_session_key(server)


def _mcp_set_status(server_name: str, status: str, *, detail: str | None = None, tool_count: int | None = None) -> None:
    MCP_SERVER_STATUS[server_name] = {
        "status": status,
        "detail": detail,
        "tool_count": tool_count,
        "updated_at": time.time(),
    }


def _mcp_invalidate_server(server: Json, *, reason: str | None = None) -> None:
    key = _mcp_cache_key(server)
    MCP_TOOL_CATALOG_CACHE.pop(key, None)
    server_name = str(server.get("name") or "")
    if server_name:
        _mcp_set_status(server_name, "invalidated", detail=reason)


def _mcp_health_snapshot(*, probe: bool = False) -> list[Json]:
    result = []
    for server in _enabled_mcp_servers():
        name = str(server.get("name") or "")
        status = MCP_SERVER_STATUS.get(name, {})
        key = _mcp_cache_key(server)
        entry: Json = {
            "name": name,
            "status": status.get("status", "unknown"),
            "detail": status.get("detail"),
            "tool_count": status.get("tool_count"),
            "session": "connected" if key in MCP_SESSIONS else "not_connected",
            "cache": "hit" if key in MCP_TOOL_CATALOG_CACHE else "miss",
        }
        if probe:
            try:
                tools = _mcp_list_server_tools(server)
                entry["probe_ok"] = True
                entry["status"] = "ok"
                entry["tool_count"] = len(tools)
                entry["session"] = "connected" if key in MCP_SESSIONS else "not_connected"
                entry["cache"] = "hit" if key in MCP_TOOL_CATALOG_CACHE else "miss"
            except Exception as exc:
                entry["probe_ok"] = False
                entry["probe_error"] = str(exc)
                entry["status"] = "broken"
                entry["tool_count"] = 0
                entry["session"] = "not_connected"
                entry["cache"] = "miss"
        result.append(entry)
    return result


def _mcp_list_server_tools(server: Json) -> list[Json]:
    key = _mcp_cache_key(server)
    ttl = _mcp_catalog_ttl(server)
    cached = MCP_TOOL_CATALOG_CACHE.get(key)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    server_name = str(server.get("name") or "")
    try:
        session = _mcp_get_session(server)
        result = session.request("tools/list")
    except Exception as exc:
        if server_name:
            _mcp_set_status(server_name, "broken", detail=str(exc), tool_count=0)
        MCP_TOOL_CATALOG_CACHE.pop(key, None)
        MCP_SESSIONS.pop(key, None)
        raise
    tools = result.get("tools") or []
    MCP_TOOL_CATALOG_CACHE[key] = (time.time(), tools)
    if server_name:
        _mcp_set_status(server_name, "ok", tool_count=len(tools))
    return tools


def _mcp_call_tool(server: Json, tool_name: str, arguments: Json) -> str:
    _mcp_validate_service_file_arguments(server, arguments, tool_name=tool_name)
    session = _mcp_get_session(server)
    result = session.request("tools/call", {"name": tool_name, "arguments": arguments})
    return _mcp_content_to_text(result)


def _mcp_content_to_text(result: Json) -> str:
    content = result.get("content") or []
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "image":
                parts.append("[image]")
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _mcp_tool_schemas(path: str) -> list[Json]:
    schemas = []
    for server in _enabled_mcp_servers():
        server_name = str(server.get("name") or "")
        try:
            for tool in _mcp_list_server_tools(server):
                tool_name = str(tool.get("name") or "")
                description = tool.get("description") or f"MCP tool from {server_name}"
                parameters = tool.get("inputSchema") or {"type": "object", "properties": {}}
                for name in (_mcp_public_name(server_name, tool_name), _mcp_legacy_public_name(server_name, tool_name)):
                    if "/messages" in path:
                        schemas.append({"name": name, "description": description, "input_schema": parameters})
                    else:
                        schemas.append({
                            "type": "function",
                            "function": {"name": name, "description": description, "parameters": parameters},
                        })
        except Exception as exc:
            _mcp_set_status(server_name, "error", detail=str(exc))
    return schemas


def _tool_name_from_schema(path: str, item: Json) -> str | None:
    if "/messages" in path:
        return item.get("name")
    func = item.get("function")
    if isinstance(func, dict):
        return func.get("name")
    return item.get("name")


def _tool_schema_for_path(path: str, tool: "GatewayTool") -> Json:
    if "/messages" in path:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
