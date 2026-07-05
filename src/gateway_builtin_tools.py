#!/usr/bin/env python3
"""Builtin tool/runtime implementations for the gateway.

This module intentionally keeps tool executors separate from HTTP/protocol
handling. Shared configuration, MCP, and runtime behavior are imported from the
split gateway modules instead of the old monolithic gateway_app.
"""
from __future__ import annotations

import ast
import base64
import concurrent.futures
import contextvars
import datetime as _dt
import glob
import html
import json
import logging
import math
import os
import pathlib
import re
import select
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable
from dataclasses import dataclass

from .gateway_errors import ToolExecutionError, ToolResult
from .gateway_mcp import (
    McpSession,
    _enabled_mcp_servers,
    _mcp_safe_component,
    _mcp_session_key,
    _mcp_use_pool,
    _mcp_get_session,
    _mcp_with_server,
    _mcp_request,
    _mcp_public_name,
    _mcp_legacy_public_name,
    _mcp_parse_public_name,
    _mcp_server_by_name,
    _mcp_list_server_tools,
    _mcp_call_tool,
    _mcp_validate_service_file_arguments,
)
from .gateway_http_actions import _http_action_opener, _validate_action_url
from .gateway_config import _config_env

_logger = logging.getLogger(__name__)

Json = dict[str, Any]

_WORKSPACE_ROOT_OVERRIDE: contextvars.ContextVar[pathlib.Path | None] = contextvars.ContextVar(
    "gateway_workspace_root_override",
    default=None,
)
_RUNTIME_SCOPE_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gateway_runtime_scope_override",
    default=None,
)
_CLIENT_ID_SCOPE_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gateway_client_id_scope_override",
    default=None,
)

@dataclass(frozen=True)
class GatewayTool:
    name: str
    description: str
    parameters: Json
    handler: Callable[[Json], str]
    risk: str = "pure"
    aliases: tuple[str, ...] = ()

@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Json
    raw: Json

def _workspace_root():
    """Get the workspace root.

    SECURITY: Returns client-provided workspace or isolated anonymous space.
    Never returns Gateway server directories.
    """
    override = _WORKSPACE_ROOT_OVERRIDE.get()
    _logger.debug("_workspace_root called, override=%s", override)
    if override is not None:
        result = pathlib.Path(override).resolve()
        _logger.debug("_workspace_root returning override: %s", result)
        return result

    # Only allow explicit env var (for testing)
    env_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    if env_root:
        result = pathlib.Path(env_root).resolve()
        _logger.debug("_workspace_root returning env: %s", result)
        return result

    try:
        from .gateway_config import _gateway_config
        configured_root = str(_gateway_config().get("workspace_root") or "").strip()
    except Exception:
        configured_root = ""
    if configured_root:
        result = pathlib.Path(configured_root).expanduser().resolve(strict=False)
        _logger.debug("_workspace_root returning configured root: %s", result)
        return result

    # This should never happen as _request_workspace_root always provides a path
    # (either client workspace or anonymous space), but fail safely if it does
    _logger.debug("_workspace_root: NO WORKSPACE AVAILABLE!")
    raise ToolExecutionError(
        "No workspace context available. Internal error.",
        failure_type="internal_error"
    )

def _runtime_scope_key() -> str:
    """Return a request/client scope for long-lived in-memory tool state.

    Remote Gateway deployments can serve many users at once.  Process-global
    maps (exec sessions, spawned agents, team placeholders, pending user
    questions) must therefore never be keyed by the caller-provided id alone:
    two clients can both choose ``session_id=dev``.  The protocol still returns
    the public id unchanged, but internally the id is namespaced by the
    request/workspace scope set by gateway_tool_runtime._workspace_scope().
    """
    scoped = _RUNTIME_SCOPE_OVERRIDE.get()
    if scoped:
        return scoped
    # Fallback for direct unit tests or internal calls outside request
    # orchestration.  This remains workspace-scoped and never uses cwd.
    try:
        root = str(_workspace_root())
    except Exception:
        root = "no-workspace"
    return f"workspace:{uuid.uuid5(uuid.NAMESPACE_URL, root).hex[:24]}"

def _scoped_runtime_id(public_id: str) -> str:
    return f"{_runtime_scope_key()}:{public_id}"

def _memory_tenant_scope_key() -> str:
    """Return the request tenant key for direct Gateway Memory tool calls."""
    scoped_client = _CLIENT_ID_SCOPE_OVERRIDE.get()
    if isinstance(scoped_client, str) and scoped_client.strip():
        try:
            from .gateway_context import _stable_memory_key_part
            return _stable_memory_key_part(scoped_client) or "anonymous"
        except Exception:
            return scoped_client.strip()
    scoped_runtime = _RUNTIME_SCOPE_OVERRIDE.get()
    if isinstance(scoped_runtime, str) and scoped_runtime.startswith("tenant:"):
        rest = scoped_runtime[len("tenant:"):]
        markers = (":session:", ":conversation:", ":thread:", ":anon:", ":session_")
        positions = [rest.find(marker) for marker in markers if rest.find(marker) >= 0]
        if positions:
            return rest[: min(positions)] or "anonymous"
        return rest or "anonymous"
    return "anonymous"

def _memory_tool_session_key(args: Json) -> str:
    """Namespace public Memory writes by the current Gateway request tenant."""
    from .gateway_context import _stable_memory_key_part

    tenant = _memory_tenant_scope_key()
    raw = str(args.get("session_key") or args.get("session_id") or "manual").strip() or "manual"
    if raw.startswith("tenant:"):
        rest = raw[len("tenant:"):]
        markers = (":session:", ":conversation:", ":thread:", ":anon:", ":session_")
        positions = [(rest.find(marker), marker) for marker in markers if rest.find(marker) >= 0]
        if positions:
            pos, marker = min(positions, key=lambda item: item[0])
            suffix = rest[pos + len(marker):]
            label = "session" if marker == ":session_" else marker.strip(":")
            return f"tenant:{tenant}:{label}:{_stable_memory_key_part(suffix) or 'manual'}"
    return f"tenant:{tenant}:session:{_stable_memory_key_part(raw) or 'manual'}"

def _resolve_workspace_path(value: str | None, *, default: str = ".") -> pathlib.Path:
    root = _workspace_root().resolve()
    raw = pathlib.Path(value or default)
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolExecutionError(
            f"path escapes workspace root: {value or default}",
            failure_type="permission_denied",
        ) from exc
    return resolved

def _require_write_enabled():
    from .gateway_config import _gateway_config
    if not _gateway_config().get("allow_write_tools", False):
        raise ToolExecutionError("write tools are disabled", failure_type="permission_denied")

def _require_shell_enabled():
    from .gateway_config import _gateway_config
    if not _gateway_config().get("allow_shell_tools", False):
        raise ToolExecutionError("shell tools are disabled", failure_type="permission_denied")

def _parse_json_arguments(raw: Any, *, allow_text: bool = False) -> Json:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        if allow_text:
            return {"text": raw}
    return {}

def _execute_tool_call(call: ToolCall, provider: str | None = None) -> "ToolResult":
    from .gateway_tool_runtime import _execute_tool_call as _impl
    return _impl(call, provider)

def _response_text(path: str, response: Json) -> str:
    from .gateway_protocol import _text_from_content
    if "/messages" in path:
        content = response.get("content") or []
        return _text_from_content(content)
    choices = response.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        return _text_from_content(message.get("content"))
    return ""

def _chunk_text_by_tokens(text: str, chunk_tokens: int, max_chunks: int) -> list[str]:
    from .gateway_context import _chunk_text_by_tokens as _chunk
    return _chunk(text, chunk_tokens, max_chunks)

def _json_schema(properties: Json, required: list[str] | None = None) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": True,
    }

def _safe_calculate(expression: str) -> float | int:
    operators: dict[type[ast.AST], Callable[..., Any]] = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: a**b,
        ast.USub: lambda a: -a,
        ast.UAdd: lambda a: +a,
    }
    allowed_names = {"pi": math.pi, "e": math.e, "tau": math.tau}

    def eval_node(node: ast.AST) -> float | int:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name) and node.id in allowed_names:
            return allowed_names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](eval_node(node.left), eval_node(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](eval_node(node.operand))
        raise ToolExecutionError("calculator expression contains unsupported syntax", failure_type="invalid_input")

    return eval_node(ast.parse(expression, mode="eval"))

def _tool_echo_probe(args: Json) -> str:
    return str(args.get("value", ""))

def _tool_calculator(args: Json) -> str:
    expression = str(args.get("expression") or args.get("input") or args.get("text") or args.get("value") or "")
    if not expression.strip():
        raise ToolExecutionError("missing expression", failure_type="invalid_input")
    result = _safe_calculate(expression)
    if isinstance(result, float) and result.is_integer():
        return str(int(result))
    return str(result)

def _tool_current_time(args: Json) -> str:
    timezone = str(args.get("timezone") or "UTC").upper()
    now = _dt.datetime.now(_dt.timezone.utc)
    if timezone in {"LOCAL", "SYSTEM"}:
        now = _dt.datetime.now().astimezone()
    return now.isoformat()

def _tool_read(args: Json) -> str:
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    if not path.is_file():
        raise ToolExecutionError(f"file not found: {path}", failure_type="not_found")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = int(args.get("offset") or 1)
    limit = args.get("limit")
    if limit is None and not args.get("full"):
        limit = int(os.environ.get("GATEWAY_READ_DEFAULT_LIMIT") or "2000")
    start = max(offset - 1, 0)
    end = len(lines) if limit is None else min(start + int(limit), len(lines))
    numbered = [f"{idx + 1}: {line}" for idx, line in enumerate(lines[start:end], start=start)]
    if end < len(lines):
        numbered.append(
            f"[gateway: file has {len(lines)} lines; returned {start + 1}-{end}. "
            f"Call Read with offset={end + 1}, limit={limit or int(os.environ.get('GATEWAY_READ_DEFAULT_LIMIT') or '2000')} for next chunk, or full=true to force full read.]"
        )
    return "\n".join(numbered)

def _tool_read_many_files(args: Json) -> str:
    raw_paths = args.get("paths") or args.get("files") or args.get("file_paths") or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    if not isinstance(raw_paths, list):
        raise ToolExecutionError("paths must be a list", failure_type="invalid_input")
    max_files = max(1, min(int(args.get("max_files") or len(raw_paths) or 20), 100))
    max_bytes = max(100, int(args.get("max_bytes_per_file") or args.get("max_bytes") or 80_000))
    outputs: list[str] = []
    for raw_path in raw_paths[:max_files]:
        path = _resolve_workspace_path(str(raw_path))
        if not path.is_file():
            outputs.append(f"## {raw_path}\nERROR: not_found")
            continue
        data = path.read_bytes()[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        rel = path.relative_to(_workspace_root().resolve())
        truncated = " [truncated]" if path.stat().st_size > max_bytes else ""
        outputs.append(f"## {rel}{truncated}\n{text}")
    return "\n\n".join(outputs)

def _tool_file_info(args: Json) -> str:
    path = _resolve_workspace_path(str(args.get("path") or args.get("file_path") or "."))
    if not path.exists():
        raise ToolExecutionError(f"path not found: {path}", failure_type="not_found")
    stat = path.stat()
    payload = {
        "path": str(path.relative_to(_workspace_root().resolve())),
        "type": "directory" if path.is_dir() else "file" if path.is_file() else "other",
        "bytes": stat.st_size,
        "modified_at": _dt.datetime.fromtimestamp(stat.st_mtime, _dt.timezone.utc).isoformat(),
        "mode": oct(stat.st_mode & 0o777),
    }
    return json.dumps(payload, ensure_ascii=False)

def _tool_list_dir(args: Json) -> str:
    path = _resolve_workspace_path(str(args.get("path") or "."))
    if not path.is_dir():
        raise ToolExecutionError(f"directory not found: {path}", failure_type="not_found")
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        suffix = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{suffix}")
    return "\n".join(entries)


def _tool_tree(args: Json) -> str:
    base = _resolve_workspace_path(str(args.get("path") or "."))
    if not base.is_dir():
        raise ToolExecutionError(f"directory not found: {base}", failure_type="not_found")
    max_depth = max(1, min(int(args.get("max_depth") or args.get("depth") or 3), 12))
    max_entries = max(1, min(int(args.get("max_entries") or args.get("limit") or 500), 5000))
    rows: list[str] = [str(base.relative_to(_workspace_root()) if base != _workspace_root() else ".") + "/"]
    count = 0

    def walk(path: pathlib.Path, prefix: str, depth: int) -> None:
        nonlocal count
        if depth >= max_depth or count >= max_entries:
            return
        children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for idx, child in enumerate(children):
            if count >= max_entries:
                return
            connector = "└── " if idx == len(children) - 1 else "├── "
            suffix = "/" if child.is_dir() else ""
            rows.append(prefix + connector + child.name + suffix)
            count += 1
            if child.is_dir():
                walk(child, prefix + ("    " if idx == len(children) - 1 else "│   "), depth + 1)

    walk(base, "", 0)
    if count >= max_entries:
        rows.append(f"[gateway: truncated after {max_entries} entries]")
    return "\n".join(rows)

def _tool_create_directory(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("path") or args.get("dir") or args.get("directory") or ""))
    path.mkdir(parents=bool(args.get("parents", True)), exist_ok=bool(args.get("exist_ok", True)))
    return f"created directory {path.relative_to(_workspace_root().resolve())}"

def _tool_delete_path(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("path") or args.get("file_path") or ""))
    if not path.exists():
        raise ToolExecutionError(f"path not found: {path}", failure_type="not_found")
    if path.is_dir():
        if path.resolve() == _workspace_root().resolve():
            raise ToolExecutionError("refusing to delete workspace root", failure_type="permission_denied")
        if not args.get("recursive"):
            raise ToolExecutionError("refusing to delete directory without recursive=true", failure_type="invalid_input")
        for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        path.rmdir()
    else:
        path.unlink()
    return f"deleted {path.relative_to(_workspace_root().resolve())}"

def _tool_move_path(args: Json) -> str:
    _require_write_enabled()
    src = _resolve_workspace_path(str(args.get("source") or args.get("src") or args.get("from") or args.get("path") or ""))
    dst = _resolve_workspace_path(str(args.get("destination") or args.get("dest") or args.get("to") or ""))
    if not src.exists():
        raise ToolExecutionError(f"source not found: {src}", failure_type="not_found")
    if dst.exists() and not args.get("overwrite"):
        raise ToolExecutionError(f"destination exists: {dst}", failure_type="invalid_input")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    return f"moved {src.relative_to(_workspace_root().resolve())} to {dst.relative_to(_workspace_root().resolve())}"

def _tool_copy_path(args: Json) -> str:
    _require_write_enabled()
    src = _resolve_workspace_path(str(args.get("source") or args.get("src") or args.get("from") or args.get("path") or ""))
    dst = _resolve_workspace_path(str(args.get("destination") or args.get("dest") or args.get("to") or ""))
    if not src.is_file():
        raise ToolExecutionError(f"source file not found: {src}", failure_type="not_found")
    if dst.exists() and not args.get("overwrite"):
        raise ToolExecutionError(f"destination exists: {dst}", failure_type="invalid_input")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return f"copied {src.relative_to(_workspace_root().resolve())} to {dst.relative_to(_workspace_root().resolve())}"

def _tool_glob(args: Json) -> str:
    pattern = str(args.get("pattern") or "**/*")
    # Strip leading / to avoid treating as absolute path
    if pattern.startswith("/"):
        pattern = pattern[1:]
    base = _resolve_workspace_path(str(args.get("path") or "."))
    root = _workspace_root().resolve()
    matches = []
    for match in glob.glob(str(base / pattern), recursive=True):
        path = pathlib.Path(match).resolve()
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        matches.append(str(rel) + ("/" if path.is_dir() else ""))
    return "\n".join(sorted(matches)[: int(args.get("limit") or 500)])

def _tool_grep(args: Json) -> str:
    pattern = str(args.get("pattern") or args.get("query") or "")
    if not pattern:
        raise ToolExecutionError("missing pattern", failure_type="invalid_input")
    base = _resolve_workspace_path(str(args.get("path") or "."))
    include = str(args.get("include") or args.get("glob") or "**/*")
    regex = re.compile(pattern)
    limit = int(args.get("limit") or 200)
    results: list[str] = []
    files = [pathlib.Path(p) for p in glob.glob(str(base / include), recursive=True)]
    for file_path in files:
        if len(results) >= limit:
            break
        if not file_path.is_file():
            continue
        try:
            file_path.resolve().relative_to(_workspace_root().resolve())
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = file_path.resolve().relative_to(_workspace_root().resolve())
                results.append(f"{rel}:{line_no}: {line}")
                if len(results) >= limit:
                    break
    return "\n".join(results)

def _tool_write(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    content = str(args.get("content") if args.get("content") is not None else args.get("file_text") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {path.relative_to(_workspace_root().resolve())}"

def _tool_edit(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    old = str(args.get("old_string") or args.get("old") or "")
    new = str(args.get("new_string") or args.get("new") or "")
    replace_all = bool(args.get("replace_all"))
    if not old:
        raise ToolExecutionError("missing old_string", failure_type="invalid_input")
    text = path.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        raise ToolExecutionError("old_string not found", failure_type="not_found")
    count = text.count(old) if replace_all else 1
    path.write_text(text.replace(old, new, -1 if replace_all else 1), encoding="utf-8")
    return f"edited {path.relative_to(_workspace_root().resolve())}; replacements={count}"

def _tool_multiedit(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    edits = args.get("edits")
    if not isinstance(edits, list):
        raise ToolExecutionError("missing edits list", failure_type="invalid_input")
    text = path.read_text(encoding="utf-8", errors="replace")
    count = 0
    for edit in edits:
        if not isinstance(edit, dict):
            raise ToolExecutionError("each edit must be an object", failure_type="invalid_input")
        old = str(edit.get("old_string") or edit.get("old") or "")
        new = str(edit.get("new_string") or edit.get("new") or "")
        replace_all = bool(edit.get("replace_all"))
        if old not in text:
            raise ToolExecutionError(f"old_string not found for edit {count + 1}", failure_type="not_found")
        text = text.replace(old, new, -1 if replace_all else 1)
        count += 1
    path.write_text(text, encoding="utf-8")
    return f"applied {count} edits to {path.relative_to(_workspace_root().resolve())}"


def _tool_regex_edit(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    pattern = str(args.get("pattern") or args.get("regex") or "")
    replacement = str(args.get("replacement") if args.get("replacement") is not None else args.get("new_string") or "")
    if not pattern:
        raise ToolExecutionError("missing regex pattern", failure_type="invalid_input")
    flags = re.MULTILINE | (re.IGNORECASE if args.get("ignore_case") else 0)
    text = path.read_text(encoding="utf-8", errors="replace")
    new_text, count = re.subn(pattern, replacement, text, 0 if args.get("replace_all", True) else 1, flags=flags)
    if count == 0:
        raise ToolExecutionError("regex pattern not found", failure_type="not_found")
    path.write_text(new_text, encoding="utf-8")
    return f"regex edited {path.relative_to(_workspace_root().resolve())}; replacements={count}"

def _tool_shell(args: Json) -> str:
    _require_shell_enabled()
    command = str(args.get("command") or args.get("cmd") or "")
    if not command:
        raise ToolExecutionError("missing command", failure_type="invalid_input")
    timeout = float(args.get("timeout") or os.environ.get("GATEWAY_SHELL_TIMEOUT", "30"))
    cwd = _resolve_workspace_path(str(args.get("cwd") or args.get("workdir") or "."))
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = []
    output.append(f"exit_code={completed.returncode}")
    if completed.stdout:
        output.append("stdout:\n" + completed.stdout)
    if completed.stderr:
        output.append("stderr:\n" + completed.stderr)
    return "\n".join(output)


def _tool_code_interpreter(args: Json) -> str:
    _require_shell_enabled()
    raw_code = str(
        args.get("code")
        or args.get("input")
        or args.get("script")
        or args.get("source")
        or args.get("description")
        or ""
    )
    fence = re.search(r"```(?:python|py)?\s*(.*?)```", raw_code, flags=re.I | re.S)
    code = fence.group(1).strip() if fence else raw_code
    if not code.strip():
        raise ToolExecutionError("missing code", failure_type="invalid_input")
    language = str(args.get("language") or "python").lower()
    if language not in {"python", "python3", "py"}:
        raise ToolExecutionError(f"unsupported code interpreter language: {language}", failure_type="invalid_input")
    timeout = float(args.get("timeout") or os.environ.get("GATEWAY_CODE_INTERPRETER_TIMEOUT", "30"))
    cwd = _resolve_workspace_path(str(args.get("cwd") or args.get("workdir") or "."))
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return json.dumps(
        {
            "language": "python",
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
        ensure_ascii=False,
        indent=2,
    )

def _read_exec_available(proc: subprocess.Popen, timeout: float = 0.05, max_bytes: int = 200_000) -> str:
    output = bytearray()
    stdout = proc.stdout
    if stdout is None:
        return ""
    while len(output) < max_bytes:
        ready, _, _ = select.select([stdout], [], [], timeout)
        if not ready:
            break
        chunk = os.read(stdout.fileno(), min(8192, max_bytes - len(output)))
        if not chunk:
            break
        output.extend(chunk)
        timeout = 0
    return output.decode("utf-8", errors="replace")

def _tool_exec_shell_start(args: Json) -> str:
    _require_shell_enabled()
    command = str(args.get("command") or args.get("cmd") or args.get("shell") or "")
    if not command:
        raise ToolExecutionError("missing command", failure_type="invalid_input")
    cwd = _resolve_workspace_path(str(args.get("cwd") or args.get("workdir") or "."))
    session_id = str(args.get("session_id") or f"exec_{uuid.uuid4().hex[:12]}")
    scoped_session_id = _scoped_runtime_id(session_id)
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    with EXEC_SESSIONS_LOCK:
        EXEC_SESSIONS[scoped_session_id] = proc
    output = _read_exec_available(proc, float(args.get("read_timeout") or 0.05))
    return json.dumps({"session_id": session_id, "pid": proc.pid, "running": proc.poll() is None, "output": output}, ensure_ascii=False)

def _exec_session(args: Json) -> tuple[str, subprocess.Popen]:
    session_id = str(args.get("session_id") or args.get("id") or "")
    if not session_id:
        raise ToolExecutionError("missing session_id", failure_type="invalid_input")
    scoped_session_id = _scoped_runtime_id(session_id)
    with EXEC_SESSIONS_LOCK:
        proc = EXEC_SESSIONS.get(scoped_session_id)
    if not proc:
        raise ToolExecutionError(f"exec session not found: {session_id}", failure_type="not_found")
    return session_id, proc

def _tool_write_stdin(args: Json) -> str:
    session_id, proc = _exec_session(args)
    if proc.poll() is not None:
        output = _read_exec_available(proc, 0)
        return json.dumps({"session_id": session_id, "running": False, "exit_code": proc.returncode, "output": output}, ensure_ascii=False)
    chars = str(args.get("chars") if args.get("chars") is not None else args.get("input") or "")
    if chars and proc.stdin:
        proc.stdin.write(chars.encode("utf-8"))
        proc.stdin.flush()
    output = _read_exec_available(proc, float(args.get("read_timeout") or 0.1))
    return json.dumps({"session_id": session_id, "running": proc.poll() is None, "output": output}, ensure_ascii=False)

def _tool_exec_wait(args: Json) -> str:
    session_id, proc = _exec_session(args)
    try:
        proc.wait(timeout=float(args.get("timeout") or 30))
    except subprocess.TimeoutExpired:
        output = _read_exec_available(proc, 0)
        return json.dumps({"session_id": session_id, "running": True, "timeout": True, "output": output}, ensure_ascii=False)
    output = _read_exec_available(proc, 0)
    with EXEC_SESSIONS_LOCK:
        EXEC_SESSIONS.pop(_scoped_runtime_id(session_id), None)
    return json.dumps({"session_id": session_id, "running": False, "exit_code": proc.returncode, "output": output}, ensure_ascii=False)

def _tool_exec_kill(args: Json) -> str:
    session_id, proc = _exec_session(args)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=float(args.get("timeout") or 2))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    output = _read_exec_available(proc, 0)
    with EXEC_SESSIONS_LOCK:
        EXEC_SESSIONS.pop(_scoped_runtime_id(session_id), None)
    return json.dumps({"session_id": session_id, "running": False, "exit_code": proc.returncode, "output": output}, ensure_ascii=False)

def _tool_git(args: Json) -> str:
    action = str(args.get("action") or args.get("subcommand") or "status").lower()
    allowed: dict[str, list[str]] = {
        "status": ["status", "--short", "--branch"],
        "diff": ["diff"],
        "log": ["log", "--oneline", "--decorate", "-n", str(int(args.get("limit") or 20))],
        "show": ["show", "--stat", "--oneline", str(args.get("rev") or args.get("ref") or "HEAD")],
        "branch": ["branch", "--show-current"],
    }
    if action not in allowed:
        raise ToolExecutionError(f"unsupported git action: {action}", failure_type="invalid_input")
    cmd = ["git", *allowed[action]]
    if action == "diff":
        if args.get("cached") or args.get("staged"):
            cmd.append("--cached")
        path = args.get("path") or args.get("file_path")
        if path:
            cmd.extend(["--", str(_resolve_workspace_path(str(path)).relative_to(_workspace_root()))])
    completed = subprocess.run(
        cmd,
        cwd=str(_workspace_root()),
        text=True,
        capture_output=True,
        timeout=float(args.get("timeout") or 20),
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise ToolExecutionError(output.strip() or "git command failed", failure_type="execution_failed")
    return output.strip()


def _tool_json_query(args: Json) -> str:
    raw = args.get("data")
    if raw is None:
        path_value = args.get("file_path") or args.get("path")
        if not path_value:
            raise ToolExecutionError("missing data or file_path", failure_type="invalid_input")
        raw = _resolve_workspace_path(str(path_value)).read_text(encoding="utf-8", errors="replace")
    data = json.loads(raw) if isinstance(raw, str) else raw
    query = str(args.get("query") or args.get("path_query") or args.get("jq") or "")
    if query in {"", "."}:
        return json.dumps(data, ensure_ascii=False, indent=2)
    current: Any = data
    for part in query.strip(".").split("."):
        if not part:
            continue
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise ToolExecutionError(f"cannot descend into {type(current).__name__}", failure_type="invalid_input")
    return json.dumps(current, ensure_ascii=False, indent=2)


def _tool_python_symbols(args: Json) -> str:
    path = _resolve_workspace_path(str(args.get("file_path") or args.get("path") or ""))
    if not path.is_file():
        raise ToolExecutionError(f"file not found: {path}", failure_type="not_found")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        raise ToolExecutionError(f"python syntax error: {exc}", failure_type="invalid_input") from exc
    symbols: list[Json] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.append({"kind": "class", "name": node.name, "line": node.lineno})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({"kind": "function", "name": node.name, "line": node.lineno, "async": isinstance(node, ast.AsyncFunctionDef)})
        elif isinstance(node, ast.Import):
            symbols.append({"kind": "import", "name": ", ".join(alias.name for alias in node.names), "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            symbols.append({"kind": "import_from", "name": "." * node.level + (node.module or ""), "items": [alias.name for alias in node.names], "line": node.lineno})
    symbols.sort(key=lambda item: int(item.get("line") or 0))
    return json.dumps({"file": str(path.relative_to(_workspace_root())), "symbols": symbols}, ensure_ascii=False, indent=2)

def _tool_apply_patch(args: Json) -> str:
    _require_write_enabled()
    patch = str(args.get("patch") or args.get("input") or args.get("diff") or "")
    if not patch.strip():
        raise ToolExecutionError("missing patch", failure_type="invalid_input")
    apply_patch_bin = os.environ.get("GATEWAY_APPLY_PATCH_BIN") or "apply_patch"
    completed = subprocess.run(
        [apply_patch_bin],
        input=patch,
        cwd=str(_workspace_root()),
        text=True,
        capture_output=True,
        timeout=float(os.environ.get("GATEWAY_APPLY_PATCH_TIMEOUT", "30")),
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise ToolExecutionError(output.strip() or "apply_patch failed", failure_type="execution_failed")
    return output.strip() or "patch applied"


def _network_tool_url_policy() -> Json:
    allow_private = os.environ.get("GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        from .gateway_config import _gateway_config
        allow_private = allow_private or bool(_gateway_config().get("allow_private_network_tools", False))
    except Exception:
        pass
    return {"allow_private_network": allow_private}


def _tool_fetch_url(args: Json) -> str:
    url = str(args.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ToolExecutionError("url must start with http:// or https://", failure_type="invalid_input")
    url_policy = _network_tool_url_policy()
    _validate_action_url(url, url_policy)
    timeout = float(args.get("timeout") or os.environ.get("GATEWAY_FETCH_TIMEOUT", "20"))
    headers = {"user-agent": "ToolCallGateway/1.0"}
    if isinstance(args.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in args["headers"].items()})
    data: bytes | None = None
    if args.get("json") is not None or args.get("body_json") is not None:
        data = json.dumps(args.get("json") if args.get("json") is not None else args.get("body_json"), ensure_ascii=False).encode("utf-8")
        headers.setdefault("content-type", "application/json")
    elif args.get("form") is not None and isinstance(args.get("form"), dict):
        data = urllib.parse.urlencode(args["form"]).encode("utf-8")
        headers.setdefault("content-type", "application/x-www-form-urlencoded")
    elif args.get("body") is not None:
        body = args.get("body")
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
    method = str(args.get("method") or ("POST" if data is not None else "GET")).upper()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _http_action_opener(url_policy).open(req, timeout=timeout) as resp:
        data = resp.read(int(args.get("max_bytes") or 200_000))
        content_type = resp.headers.get("content-type", "")
        status = resp.status
        final_url = resp.geturl()
    text = data.decode("utf-8", errors="replace")
    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    if title_match:
        title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_match.group(1)))).strip()
    prefix = [f"status: {status}", f"content-type: {content_type}", f"url: {final_url}"]
    if title:
        prefix.append(f"title: {title}")
    return "\n".join(prefix) + "\n\n" + text

def _tool_web_search(args: Json) -> str:
    query = str(args.get("query") or args.get("q") or args.get("search") or "")
    if not query.strip():
        raise ToolExecutionError("missing query", failure_type="invalid_input")
    max_results = max(1, min(int(args.get("max_results") or args.get("limit") or 5), 10))
    base_url = str(args.get("search_url") or os.environ.get("GATEWAY_SEARCH_URL") or "https://duckduckgo.com/html/")
    separator = "&" if "?" in base_url else "?"
    url = base_url + separator + urllib.parse.urlencode({"q": query})
    url_policy = _network_tool_url_policy()
    _validate_action_url(url, url_policy)
    req = urllib.request.Request(
        url,
        headers={
            "user-agent": "ToolCallGateway/1.0 (+https://localhost)",
            "accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with _http_action_opener(url_policy).open(req, timeout=float(args.get("timeout") or os.environ.get("GATEWAY_SEARCH_TIMEOUT", "15"))) as resp:
            html_text = resp.read(int(args.get("max_bytes") or 500_000)).decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise ToolExecutionError(f"web search connection failed: {exc.reason}", failure_type="execution_failed") from exc
    results: list[Json] = []
    for match in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html_text,
        flags=re.I | re.S,
    ):
        href = html.unescape(match.group(1))
        if "uddg=" in href:
            parsed = urllib.parse.urlparse(href)
            query_params = urllib.parse.parse_qs(parsed.query)
            href = query_params.get("uddg", [href])[0]
        title = re.sub(r"<[^>]+>", "", match.group(2))
        title = html.unescape(re.sub(r"\s+", " ", title)).strip()
        tail = html_text[match.end() : match.end() + 1200]
        snippet_match = re.search(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', tail, flags=re.I | re.S)
        snippet = ""
        if snippet_match:
            snippet = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", snippet_match.group(1)))).strip()
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break
    if not results:
        title_matches = re.findall(r"<title>(.*?)</title>", html_text, flags=re.I | re.S)
        detail = html.unescape(title_matches[0]) if title_matches else "no parseable results"
        return json.dumps({"query": query, "results": [], "detail": detail}, ensure_ascii=False, indent=2)
    return json.dumps({"query": query, "results": results}, ensure_ascii=False, indent=2)

def _tool_todo_write(args: Json) -> str:
    todos = args.get("todos") or args.get("items") or []
    if not isinstance(todos, list):
        raise ToolExecutionError("todos must be a list", failure_type="invalid_input")
    return json.dumps({"ok": True, "todos": todos}, ensure_ascii=False)

def _tool_update_plan(args: Json) -> str:
    plan = args.get("plan") or args.get("items") or []
    return json.dumps({"ok": True, "plan": plan}, ensure_ascii=False)

def _tool_notebook_edit(args: Json) -> str:
    _require_write_enabled()
    path = _resolve_workspace_path(str(args.get("notebook_path") or args.get("file_path") or args.get("path") or ""))
    if path.suffix != ".ipynb":
        raise ToolExecutionError("NotebookEdit requires a .ipynb file", failure_type="invalid_input")
    notebook = json.loads(path.read_text(encoding="utf-8"))
    cells = notebook.setdefault("cells", [])
    if not isinstance(cells, list):
        raise ToolExecutionError("notebook cells must be a list", failure_type="invalid_input")
    edit_mode = str(args.get("edit_mode") or args.get("mode") or "replace").lower()
    cell_id = args.get("cell_id")
    cell_number = args.get("cell_number") if args.get("cell_number") is not None else args.get("index")
    idx: int | None = None
    if cell_id is not None:
        for i, cell in enumerate(cells):
            if isinstance(cell, dict) and str(cell.get("id")) == str(cell_id):
                idx = i
                break
    elif cell_number is not None:
        raw_idx = int(cell_number)
        idx = raw_idx - 1 if raw_idx > 0 else raw_idx
    if edit_mode not in {"insert", "append"} and (idx is None or idx < 0 or idx >= len(cells)):
        raise ToolExecutionError("target notebook cell not found", failure_type="not_found")
    source_value = args.get("new_source")
    if source_value is None:
        source_value = args.get("source")
    if source_value is None:
        source_value = args.get("content") or args.get("text") or ""
    source = source_value if isinstance(source_value, list) else str(source_value).splitlines(keepends=True)
    cell_type = str(args.get("cell_type") or "code")
    if edit_mode in {"delete", "remove"}:
        assert idx is not None
        del cells[idx]
        action = "deleted"
    elif edit_mode in {"insert", "append"}:
        new_cell = {"cell_type": cell_type, "metadata": {}, "source": source}
        if cell_type == "code":
            new_cell.update({"execution_count": None, "outputs": []})
        insert_at = len(cells) if idx is None else max(min(idx, len(cells)), 0)
        cells.insert(insert_at, new_cell)
        action = f"inserted at {insert_at + 1}"
    else:
        assert idx is not None
        cell = cells[idx]
        if not isinstance(cell, dict):
            raise ToolExecutionError("target cell must be an object", failure_type="invalid_input")
        cell["source"] = source
        if "cell_type" in args:
            cell["cell_type"] = cell_type
        action = f"replaced cell {idx + 1}"
    path.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"{action} in {path.relative_to(_workspace_root())}"

def _tool_view_image(args: Json) -> str:
    path = _resolve_workspace_path(str(args.get("path") or args.get("file_path") or args.get("image_path") or ""))
    if not path.is_file():
        raise ToolExecutionError(f"image not found: {path}", failure_type="not_found")
    size = path.stat().st_size
    suffix = path.suffix.lower().lstrip(".")
    max_bytes = int(args.get("max_bytes") or 0)
    payload: Json = {"path": str(path.relative_to(_workspace_root())), "bytes": size, "format": suffix}
    try:
        from PIL import Image, ImageStat  # type: ignore

        with Image.open(path) as image:
            payload.update(
                {
                    "detected_format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "frames": getattr(image, "n_frames", 1),
                }
            )
            stat_image = image.convert("RGB").resize((1, 1))
            payload["average_rgb"] = list(stat_image.getpixel((0, 0)))
            if args.get("histogram"):
                thumb = image.convert("RGB")
                thumb.thumbnail((64, 64))
                stat = ImageStat.Stat(thumb)
                payload["mean_rgb"] = [round(float(v), 2) for v in stat.mean]
                payload["extrema_rgb"] = stat.extrema
    except Exception as exc:
        payload["image_parse_error"] = str(exc)
    if max_bytes > 0:
        payload["base64"] = base64.b64encode(path.read_bytes()[:max_bytes]).decode("ascii")
        payload["truncated"] = size > max_bytes
    return json.dumps(payload, ensure_ascii=False)


def _tool_intent_detect(args: Json) -> str:
    text = str(args.get("text") or args.get("input") or args.get("query") or args.get("prompt") or "")
    lowered = text.lower()
    intents: list[str] = []
    suggestions: list[Json] = []

    def add(intent: str, tool: str, reason: str, arguments: Json | None = None) -> None:
        if intent not in intents:
            intents.append(intent)
        suggestions.append({"tool": tool, "reason": reason, "arguments": arguments or {}})

    if any(k in lowered for k in ["分析", "analyze", "review", "理解", "梳理", "逐个类", "项目"]):
        add("project_analysis", "Tree", "先识别项目结构", {"path": ".", "max_depth": 3})
        add("project_analysis", "Glob", "枚举代码文件", {"pattern": "**/*.py"})
    if any(k in lowered for k in ["修改", "修复", "改", "edit", "write", "implement", "实现"]):
        add("code_change", "Read", "修改前读取目标文件")
        add("code_change", "Edit", "小范围文本替换；复杂变更使用 apply_patch")
    if any(k in lowered for k in ["运行", "测试", "报错", "build", "test", "pytest", "unittest", "执行"]):
        add("coding_execution", "Bash", "运行构建/测试/诊断命令")
    if any(k in lowered for k in ["网络", "网页", "url", "http", "https", "搜索", "search", "fetch"]):
        add("network", "WebFetch", "读取 URL 内容")
        add("network", "WebSearch", "搜索网页结果")
    if any(k in lowered for k in ["图片", "图像", "截图", "识图", "image", "vision", "screenshot"]):
        add("vision", "view_image", "读取本地图片元数据/尺寸/颜色摘要")
    if any(k in lowered for k in ["并行", "多个", "全部", "所有", "parallel", "fanout"]):
        add("parallel", "multi_tool_use.parallel", "独立工具调用可并行执行")

    paths = re.findall(r"@?([A-Za-z0-9_./\\-]+\\.(?:py|md|json|txt|yaml|yml|toml|js|ts|tsx|png|jpg|jpeg|gif|webp))", text)
    if paths:
        suggestions.append({"tool": "ReadManyFiles", "reason": "用户文本中出现具体文件路径", "arguments": {"paths": paths[:20]}})
    return json.dumps({"text": text, "intents": intents or ["general_chat"], "suggestions": suggestions}, ensure_ascii=False, indent=2)

def _mcp_servers_for_args(args: Json) -> list[Json]:
    server_filter = str(args.get("server") or args.get("server_name") or "").strip()
    servers = _enabled_mcp_servers()
    if server_filter:
        servers = [
            server
            for server in servers
            if str(server.get("name") or "") == server_filter
            or _mcp_safe_component(str(server.get("name") or ""), default="mcp") == server_filter
        ]
    return servers

def _mcp_request_server(server: Json, method: str, params: Json | None = None) -> Json:
    if method in {"resources/read", "prompts/get"}:
        _mcp_validate_service_file_arguments(server, params or {}, tool_name=method)
    if _mcp_use_pool(server):
        return _mcp_get_session(server).request(method, params or {})

    def run(proc: subprocess.Popen, timeout: float) -> Json:
        return _mcp_request(proc, method, params or {}, request_id=2, timeout=timeout)

    return _mcp_with_server(server, run)

def _tool_list_mcp_resources(args: Json) -> str:
    rows: list[Json] = []
    for server in _mcp_servers_for_args(args):
        name = str(server.get("name") or _mcp_session_key(server))
        try:
            result = _mcp_request_server(server, "resources/list", {})
            for item in result.get("resources") or []:
                if isinstance(item, dict):
                    rows.append({"server": name, **item})
        except Exception as exc:
            rows.append({"server": name, "error": str(exc)})
    return json.dumps({"resources": rows}, ensure_ascii=False, indent=2)

def _tool_list_mcp_resource_templates(args: Json) -> str:
    rows: list[Json] = []
    for server in _mcp_servers_for_args(args):
        name = str(server.get("name") or _mcp_session_key(server))
        try:
            result = _mcp_request_server(server, "resources/templates/list", {})
            for item in result.get("resourceTemplates") or result.get("templates") or []:
                if isinstance(item, dict):
                    rows.append({"server": name, **item})
        except Exception as exc:
            rows.append({"server": name, "error": str(exc)})
    return json.dumps({"resourceTemplates": rows}, ensure_ascii=False, indent=2)

def _tool_read_mcp_resource(args: Json) -> str:
    uri = str(args.get("uri") or args.get("resource_uri") or "")
    if not uri:
        raise ToolExecutionError("missing uri", failure_type="invalid_input")
    outputs: list[Json] = []
    servers = _mcp_servers_for_args(args) or _enabled_mcp_servers()
    for server in servers:
        name = str(server.get("name") or _mcp_session_key(server))
        try:
            result = _mcp_request_server(server, "resources/read", {"uri": uri})
            outputs.append({"server": name, "result": result})
            if args.get("server") or args.get("server_name"):
                break
        except Exception as exc:
            outputs.append({"server": name, "error": str(exc)})
    return json.dumps({"contents": outputs}, ensure_ascii=False, indent=2)

def _tool_mcp_get_prompt(args: Json) -> str:
    name = str(args.get("name") or args.get("prompt") or "")
    if not name:
        raise ToolExecutionError("missing prompt name", failure_type="invalid_input")
    params = {"name": name, "arguments": args.get("arguments") or args.get("args") or {}}
    outputs: list[Json] = []
    servers = _mcp_servers_for_args(args) or _enabled_mcp_servers()
    for server in servers:
        server_name = str(server.get("name") or _mcp_session_key(server))
        try:
            result = _mcp_request_server(server, "prompts/get", params)
            outputs.append({"server": server_name, "result": result})
            if args.get("server") or args.get("server_name"):
                break
        except Exception as exc:
            outputs.append({"server": server_name, "error": str(exc)})
    return json.dumps({"prompts": outputs}, ensure_ascii=False, indent=2)


def _tool_mcp_list_tools(args: Json) -> str:
    rows: list[Json] = []
    servers = _mcp_servers_for_args(args) or _enabled_mcp_servers()
    for server in servers:
        server_name = str(server.get("name") or _mcp_session_key(server))
        try:
            for tool in _mcp_list_server_tools(server):
                tool_name = str(tool.get("name") or "")
                rows.append({
                    "server": server_name,
                    "name": tool_name,
                    "gateway_name": _mcp_public_name(server_name, tool_name),
                    "legacy_name": _mcp_legacy_public_name(server_name, tool_name),
                    "description": tool.get("description"),
                    "input_schema": tool.get("inputSchema"),
                })
        except Exception as exc:
            rows.append({"server": server_name, "error": f"{type(exc).__name__}: {exc}"})
    return json.dumps({"tools": rows}, ensure_ascii=False, indent=2)


def _tool_mcp_call_tool(args: Json) -> str:
    public_name = str(args.get("public_name") or args.get("gateway_name") or args.get("tool") or "").strip()
    server_name = str(args.get("server") or args.get("server_name") or "").strip()
    tool_name = str(args.get("name") or args.get("tool_name") or "").strip()
    arguments = args.get("arguments") if isinstance(args.get("arguments"), dict) else args.get("args") if isinstance(args.get("args"), dict) else {}
    parsed = _mcp_parse_public_name(public_name) if public_name else None
    if parsed:
        server_name, tool_name = parsed
    if not server_name or not tool_name:
        raise ToolExecutionError("missing MCP server/tool name", failure_type="invalid_input")
    server = _mcp_server_by_name(server_name)
    if not server:
        raise ToolExecutionError(f"MCP server not configured or enabled: {server_name}", failure_type="connector_required")
    return _mcp_call_tool(server, tool_name, arguments)


def _tool_memory(args: Json) -> str:
    from .gateway_context import _memory_extract_keywords, _sqlite_insert_memory, _sqlite_tail_memories

    action = str(args.get("action") or args.get("operation") or "list").lower()
    workspace = str(_workspace_root())
    if action in {"list", "read", "recall"}:
        include_all = bool(args.get("all_workspaces") or args.get("include_all_workspaces"))
        if include_all:
            raise ToolExecutionError(
                "cross-workspace memory listing is available only through admin APIs",
                failure_type="permission_denied",
            )
        tenant_key = _memory_tenant_scope_key()
        return json.dumps(
            {"memories": _sqlite_tail_memories(int(args.get("limit") or 50), workspace, tenant_key=tenant_key)},
            ensure_ascii=False,
            indent=2,
        )
    if action in {"write", "remember", "add"}:
        summary = str(args.get("summary") or args.get("content") or args.get("text") or "").strip()
        if not summary:
            raise ToolExecutionError("missing memory summary/content", failure_type="invalid_input")
        keywords = args.get("keywords") if isinstance(args.get("keywords"), list) else _memory_extract_keywords(summary)
        _sqlite_insert_memory(
            _memory_tool_session_key(args),
            workspace,
            str(args.get("kind") or "manual"),
            summary,
            [str(k) for k in keywords],
            str(args.get("source_request_id") or "manual"),
            int(args.get("importance") or 2),
        )
        return json.dumps({"ok": True, "stored": True, "workspace_root": workspace, "keywords": keywords}, ensure_ascii=False)
    raise ToolExecutionError(f"unsupported memory action: {action}", failure_type="invalid_input")


def _tool_multi_tool_use_parallel(args: Json) -> str:
    tool_uses = args.get("tool_uses") or args.get("calls") or []
    if not isinstance(tool_uses, list):
        raise ToolExecutionError("tool_uses must be a list", failure_type="invalid_input")
    try:
        workspace = _workspace_root()
    except ToolExecutionError:
        workspace = None

    def run_one(index_and_item: tuple[int, Any]) -> Json:
        token = _WORKSPACE_ROOT_OVERRIDE.set(workspace)
        try:
            return run_one_scoped(index_and_item)
        finally:
            _WORKSPACE_ROOT_OVERRIDE.reset(token)

    def run_one_scoped(index_and_item: tuple[int, Any]) -> Json:
        index, item = index_and_item
        if not isinstance(item, dict):
            return {"index": index, "success": False, "failure_type": "invalid_input", "content": "tool use must be object"}
        recipient = str(item.get("recipient_name") or item.get("name") or item.get("tool_name") or "")
        name = recipient.rsplit(".", 1)[-1] if recipient else ""
        if name in {"parallel", "multi_tool_use.parallel"}:
            return {"index": index, "name": name, "success": False, "failure_type": "invalid_input", "content": "recursive parallel calls are disabled"}
        call = ToolCall(
            call_id=str(item.get("id") or item.get("call_id") or f"parallel_{index}_{uuid.uuid4().hex}"),
            name=name,
            arguments=_parse_json_arguments(item.get("parameters") if item.get("parameters") is not None else item.get("arguments"), allow_text=True),
            raw=item,
        )
        result = _execute_tool_call(call)
        return {
            "index": index,
            "name": result.name,
            "success": result.success,
            "failure_type": result.failure_type,
            "content": result.content,
        }

    max_workers = max(1, min(int(args.get("max_workers") or 4), 8, len(tool_uses) or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(run_one, enumerate(tool_uses)))
    return json.dumps({"results": results}, ensure_ascii=False, indent=2)

EXEC_SESSIONS: dict[str, subprocess.Popen] = {}
EXEC_SESSIONS_LOCK = threading.Lock()
AGENT_SESSIONS: dict[str, dict[str, Any]] = {}
AGENT_SESSIONS_LOCK = threading.Lock()
AGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=int(os.environ.get("GATEWAY_AGENT_SESSION_WORKERS") or "4"))
PENDING_USER_QUESTIONS: dict[str, Json] = {}
TEAM_SESSIONS: dict[str, Json] = {}
TEAM_SESSIONS_LOCK = threading.Lock()


def _agent_session_status(agent_id: str, item: dict[str, Any]) -> Json:
    future = item.get("future")
    status = "unknown"
    output = None
    error = None
    if isinstance(future, concurrent.futures.Future):
        if future.cancelled():
            status = "cancelled"
        elif future.done():
            try:
                output = future.result()
                status = "completed"
            except Exception as exc:  # pragma: no cover - defensive runtime path
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
        else:
            status = "running"
    return {
        "id": agent_id,
        "status": status,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "prompt_preview": str(item.get("prompt") or "")[:500],
        "output": output,
        "error": error,
    }


def _tool_spawn_agent(args: Json) -> str:
    prompt = _agent_prompt_from_args(args)
    model = args.get("model")
    agent_id = str(args.get("id") or args.get("agent_id") or f"agent_{uuid.uuid4().hex[:12]}")
    scoped_agent_id = _scoped_runtime_id(agent_id)

    def run() -> str:
        return _tool_agent({"prompt": prompt, "model": model, "chunk_tokens": args.get("chunk_tokens"), "max_chunks": args.get("max_chunks"), "max_workers": args.get("max_workers")})

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    future = AGENT_EXECUTOR.submit(run)
    with AGENT_SESSIONS_LOCK:
        AGENT_SESSIONS[scoped_agent_id] = {"future": future, "prompt": prompt, "created_at": now, "updated_at": now, "messages": [], "public_id": agent_id}
    return json.dumps({"id": agent_id, "status": "running", "object": "gateway.agent"}, ensure_ascii=False)


def _tool_send_input(args: Json) -> str:
    agent_id = str(args.get("target") or args.get("agent_id") or args.get("id") or "")
    message = str(args.get("message") or args.get("input") or args.get("text") or "")
    if not agent_id:
        raise ToolExecutionError("missing agent target/id", failure_type="invalid_input")
    scoped_agent_id = _scoped_runtime_id(agent_id)
    with AGENT_SESSIONS_LOCK:
        item = AGENT_SESSIONS.get(scoped_agent_id)
        if not item:
            raise ToolExecutionError(f"agent not found: {agent_id}", failure_type="not_found")
        item.setdefault("messages", []).append({"role": "user", "content": message, "ts": _dt.datetime.now(_dt.timezone.utc).isoformat()})
        item["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        future = item.get("future")
        if isinstance(future, concurrent.futures.Future) and future.done() and message.strip():
            prompt = str(item.get("prompt") or "") + "\n\n# Follow-up input\n" + message
            item["prompt"] = prompt
            item["future"] = AGENT_EXECUTOR.submit(lambda: _tool_agent({"prompt": prompt}))
    return json.dumps({"id": agent_id, "status": "accepted"}, ensure_ascii=False)


def _tool_wait_agent(args: Json) -> str:
    raw_targets = args.get("targets") or args.get("target") or args.get("agent_id") or args.get("id")
    if isinstance(raw_targets, str):
        targets = [raw_targets]
    elif isinstance(raw_targets, list):
        targets = [str(item) for item in raw_targets]
    else:
        targets = []
    if not targets:
        raise ToolExecutionError("missing agent target(s)", failure_type="invalid_input")
    timeout = float(args.get("timeout") or args.get("timeout_ms") or 30000)
    if timeout > 1000:
        timeout = timeout / 1000.0
    deadline = _dt.datetime.now().timestamp() + max(timeout, 0)
    results: list[Json] = []
    for agent_id in targets:
        scoped_agent_id = _scoped_runtime_id(agent_id)
        with AGENT_SESSIONS_LOCK:
            item = AGENT_SESSIONS.get(scoped_agent_id)
        if not item:
            results.append({"id": agent_id, "status": "not_found"})
            continue
        future = item.get("future")
        remaining = max(0.0, deadline - _dt.datetime.now().timestamp())
        if isinstance(future, concurrent.futures.Future) and remaining > 0:
            try:
                future.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                pass
            except Exception:
                pass
        results.append(_agent_session_status(agent_id, item))
    return json.dumps({"agents": results}, ensure_ascii=False, indent=2)


def _tool_close_agent(args: Json) -> str:
    agent_id = str(args.get("target") or args.get("agent_id") or args.get("id") or "")
    if not agent_id:
        raise ToolExecutionError("missing agent target/id", failure_type="invalid_input")
    scoped_agent_id = _scoped_runtime_id(agent_id)
    with AGENT_SESSIONS_LOCK:
        item = AGENT_SESSIONS.pop(scoped_agent_id, None)
    if not item:
        raise ToolExecutionError(f"agent not found: {agent_id}", failure_type="not_found")
    future = item.get("future")
    cancelled = bool(isinstance(future, concurrent.futures.Future) and future.cancel())
    return json.dumps({"id": agent_id, "closed": True, "cancelled": cancelled, "previous": _agent_session_status(agent_id, item)}, ensure_ascii=False)


def _tool_resume_agent(args: Json) -> str:
    agent_id = str(args.get("id") or args.get("agent_id") or args.get("target") or "")
    if not agent_id:
        raise ToolExecutionError("missing agent id", failure_type="invalid_input")
    scoped_agent_id = _scoped_runtime_id(agent_id)
    with AGENT_SESSIONS_LOCK:
        item = AGENT_SESSIONS.get(scoped_agent_id)
    if not item:
        raise ToolExecutionError(f"agent not found: {agent_id}", failure_type="not_found")
    return json.dumps(_agent_session_status(agent_id, item), ensure_ascii=False, indent=2)


def _tool_request_user_input(args: Json) -> str:
    request_id = str(args.get("id") or f"question_{uuid.uuid4().hex[:12]}")
    payload = {"id": request_id, "questions": args.get("questions") or args.get("question") or args, "status": "pending_user_input"}
    PENDING_USER_QUESTIONS[_scoped_runtime_id(request_id)] = payload
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _tool_team_create(args: Json) -> str:
    team_id = str(args.get("id") or args.get("team_id") or f"team_{uuid.uuid4().hex[:12]}")
    scoped_team_id = _scoped_runtime_id(team_id)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    payload = {
        "id": team_id,
        "status": "active",
        "name": args.get("name") or team_id,
        "members": args.get("members") or args.get("agents") or [],
        "tasks": args.get("tasks") or [],
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }
    with TEAM_SESSIONS_LOCK:
        TEAM_SESSIONS[scoped_team_id] = payload
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _tool_send_message(args: Json) -> str:
    target = str(args.get("target") or args.get("team_id") or args.get("agent_id") or "")
    message = str(args.get("message") or args.get("content") or args.get("text") or "")
    if not target:
        raise ToolExecutionError("missing message target", failure_type="invalid_input")
    scoped_target = _scoped_runtime_id(target)
    with AGENT_SESSIONS_LOCK:
        is_agent = scoped_target in AGENT_SESSIONS
    if is_agent:
        return _tool_send_input({"target": target, "message": message})
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with TEAM_SESSIONS_LOCK:
        team = TEAM_SESSIONS.get(scoped_target)
        if not team:
            raise ToolExecutionError(f"target not found: {target}", failure_type="not_found")
        item = {"from": args.get("sender") or args.get("from") or "gateway", "content": message, "ts": now}
        team.setdefault("messages", []).append(item)
        team["updated_at"] = now
    return json.dumps({"target": target, "status": "delivered", "message": item}, ensure_ascii=False, indent=2)


def _tool_team_delete(args: Json) -> str:
    team_id = str(args.get("id") or args.get("team_id") or args.get("target") or "")
    if not team_id:
        raise ToolExecutionError("missing team id", failure_type="invalid_input")
    scoped_team_id = _scoped_runtime_id(team_id)
    with TEAM_SESSIONS_LOCK:
        previous = TEAM_SESSIONS.pop(scoped_team_id, None)
    if not previous:
        raise ToolExecutionError(f"team not found: {team_id}", failure_type="not_found")
    return json.dumps({"id": team_id, "deleted": True, "previous": previous}, ensure_ascii=False, indent=2)


def _tool_lsp(args: Json) -> str:
    action = str(args.get("action") or args.get("method") or args.get("command") or "document_symbols").lower()
    file_path = str(args.get("file") or args.get("file_path") or args.get("path") or "")
    if action in {"document_symbols", "symbols", "outline", "lsp_document_symbols"}:
        return _tool_python_symbols({"file_path": file_path})
    if action in {"grep", "references", "search"}:
        return _tool_grep({"pattern": args.get("pattern") or args.get("query") or args.get("symbol") or "", "path": args.get("path") or ".", "limit": args.get("limit") or 100})
    raise ToolExecutionError(f"unsupported LSP action: {action}", failure_type="invalid_input")


def _tool_web_browser(args: Json) -> str:
    url = str(args.get("url") or args.get("href") or args.get("input") or "")
    if not url:
        raise ToolExecutionError("missing url", failure_type="invalid_input")
    return _tool_fetch_url({**args, "url": url})


def _tool_file_search_call(args: Json) -> str:
    pattern = str(args.get("pattern") or args.get("query") or args.get("text") or "")
    if not pattern:
        raise ToolExecutionError("missing search pattern/query", failure_type="invalid_input")
    return _tool_grep({"pattern": pattern, "path": args.get("path") or ".", "include": args.get("include") or args.get("glob"), "limit": args.get("limit") or 200})


def _tool_web_search_call(args: Json) -> str:
    query = str(args.get("query") or args.get("q") or args.get("text") or "")
    if not query:
        raise ToolExecutionError("missing query", failure_type="invalid_input")
    return _tool_web_search({"query": query, "max_results": args.get("max_results") or args.get("limit") or 5, "search_url": args.get("search_url")})


def _tool_plan_mode(args: Json) -> str:
    return json.dumps({"ok": True, "plan": args.get("plan") or args.get("content") or args}, ensure_ascii=False)


def _tool_goal(args: Json) -> str:
    return json.dumps({"ok": True, "goal": args}, ensure_ascii=False)


def _agent_prompt_from_args(args: Json) -> str:
    prompt = str(
        args.get("prompt")
        or args.get("description")
        or args.get("task")
        or args.get("input")
        or args.get("query")
        or ""
    )
    files = args.get("files") or args.get("paths") or []
    if isinstance(files, str):
        files = [files]
    if isinstance(files, list) and files:
        file_context = _tool_read_many_files({"paths": files, "max_files": args.get("max_files") or 50, "max_bytes_per_file": args.get("max_bytes_per_file") or 120_000})
        prompt += "\n\n# File context\n" + file_context
    if not prompt.strip():
        raise ToolExecutionError("missing agent prompt/task", failure_type="invalid_input")
    return prompt


def _agent_call_upstream(prompt: str, model: str | None = None) -> str:
    from .gateway_proxy import NativeProxyClient

    body = {
        "model": model or _config_env("UPSTREAM_MODEL", ""),
        "max_tokens": int(os.environ.get("GATEWAY_AGENT_MAX_TOKENS") or "4096"),
        "messages": [{"role": "user", "content": prompt}],
    }
    response = NativeProxyClient().forward("/v1/messages", body)
    return _response_text("/v1/messages", response) or json.dumps(response, ensure_ascii=False)[:8000]


def _tool_agent(args: Json) -> str:
    prompt = _agent_prompt_from_args(args)
    chunk_tokens = int(args.get("chunk_tokens") or os.environ.get("GATEWAY_AGENT_CHUNK_TOKENS") or "10000")
    max_chunks = int(args.get("max_chunks") or os.environ.get("GATEWAY_AGENT_MAX_CHUNKS") or "32")
    chunks = _chunk_text_by_tokens(prompt, chunk_tokens, max_chunks)
    if len(chunks) < 2:
        return _agent_call_upstream(prompt, args.get("model"))

    workers = max(1, min(int(args.get("max_workers") or os.environ.get("GATEWAY_AGENT_MAX_WORKERS") or "4"), 8, len(chunks)))

    def analyze(index_and_chunk: tuple[int, str]) -> str:
        index, chunk = index_and_chunk
        sub_prompt = (
            "你是 Gateway Agent 子任务。只分析当前片段，提取事实、类/函数职责、风险、证据和结论；不要编造。\n\n"
            f"片段 {index + 1}/{len(chunks)}:\n{chunk}"
        )
        return _agent_call_upstream(sub_prompt, args.get("model"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        partials = list(executor.map(analyze, enumerate(chunks)))
    synthesis = "\n\n".join(f"## 子分析 {i + 1}\n{part}" for i, part in enumerate(partials))
    final_prompt = (
        "你是 Gateway Agent 汇总器。请综合多个子分析，输出完整结论；冲突要标注，缺证据要说明未知。\n\n"
        f"原始任务：\n{prompt[:4000]}\n\n{synthesis}"
    )
    final = _agent_call_upstream(final_prompt, args.get("model"))
    return json.dumps({"strategy": "agent_fanout_synthesis", "chunks": len(chunks), "workers": workers, "output": final}, ensure_ascii=False, indent=2)


def _plugin_skill_dirs(workspace: pathlib.Path) -> list[pathlib.Path]:
    """Return project-local plugin skill directories declared by plugin manifests.

    Plugin manifests can name a relative ``skills`` directory.  Only paths that
    stay inside the active workspace are accepted, so a plugin installed in the
    Gateway service checkout cannot make another downstream project load its
    skills accidentally.
    """
    workspace = workspace.resolve(strict=False)
    plugin_containers = [
        workspace / ".codex" / "plugins",
        workspace / ".claude" / "plugins",
        workspace / ".opencode" / "plugins",
        workspace / "plugins",
    ]
    manifests: list[pathlib.Path] = []
    for container in plugin_containers:
        if not container.is_dir():
            continue
        for pattern in (
            "*/.codex-plugin/plugin.json",
            "*/.claude-plugin/plugin.json",
            "*/.opencode-plugin/plugin.json",
            "*/plugin.json",
        ):
            manifests.extend(container.glob(pattern))

    result: list[pathlib.Path] = []
    for manifest in manifests:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_skills = payload.get("skills") or payload.get("skill")
        if not raw_skills:
            continue
        values = raw_skills if isinstance(raw_skills, list) else [raw_skills]
        plugin_root = manifest.parent.parent if manifest.parent.name.endswith("-plugin") else manifest.parent
        for raw_value in values:
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            raw_path = pathlib.Path(raw_value).expanduser()
            candidate = raw_path if raw_path.is_absolute() else plugin_root / raw_path
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(workspace)
            except ValueError:
                continue
            result.append(resolved)
    return result


def _skill_dirs() -> list[pathlib.Path]:
    try:
        workspace = _workspace_root()
    except ToolExecutionError:
        workspace = None
    home = pathlib.Path.home()
    candidates: list[pathlib.Path] = []
    if workspace is not None:
        candidates.extend([
            # Workspace-local skills first so downstream project intelligence wins.
            workspace / ".codex" / "skills",
            workspace / ".agents" / "skills",
            workspace / ".claude" / "skills",
            workspace / ".opencode" / "skills",
            workspace / "skills",
            *_plugin_skill_dirs(workspace),
        ])
    candidates.extend([
        # Then user-global skill stores used by Codex, Claude Code, and OMX.
        home / ".codex" / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".opencode" / "skills",
    ])
    extra = os.environ.get("GATEWAY_SKILLS_DIRS")
    if extra:
        candidates.extend(pathlib.Path(part) for part in extra.split(os.pathsep) if part)
    seen: set[pathlib.Path] = set()
    existing: list[pathlib.Path] = []
    for path in candidates:
        resolved = path.expanduser().resolve(strict=False)
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        existing.append(resolved)
    return existing


def _load_skill(name: str) -> tuple[pathlib.Path, str] | None:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")
    for root in _skill_dirs():
        for candidate in (root / safe / "SKILL.md", root / name / "SKILL.md"):
            if candidate.is_file():
                return candidate, candidate.read_text(encoding="utf-8", errors="replace")
    return None


def _tool_skill(args: Json) -> str:
    name = str(args.get("name") or args.get("skill") or "").strip()
    prompt = str(args.get("prompt") or args.get("task") or args.get("input") or "")
    if not name:
        skills = []
        for root in _skill_dirs():
            for skill_file in sorted(root.glob("*/SKILL.md")):
                skills.append({"name": skill_file.parent.name, "path": str(skill_file)})
        return json.dumps({"skills": skills}, ensure_ascii=False, indent=2)
    loaded = _load_skill(name)
    if not loaded:
        raise ToolExecutionError(f"skill not found: {name}", failure_type="not_found")
    path, content = loaded
    if not prompt:
        return json.dumps({"name": name, "path": str(path), "content": content}, ensure_ascii=False, indent=2)
    return _tool_agent({"prompt": f"Complete the task following the Skill guide below.\n\n# Skill {name}\n{content}\n\n# Task\n{prompt}", "model": args.get("model")})

def _tool_connector_required(args: Json) -> str:
    name = str(args.get("_tool_name") or "tool")
    raise ToolExecutionError(
        f"{name} requires a configured connector/runtime and is not ready",
        failure_type="connector_required",
    )


# --- Real computer_use / GUI / image_generation implementations ---

def _fail_if_json_tool_error(content: str) -> str:
    try:
        payload = json.loads(content)
    except Exception:
        return content
    if isinstance(payload, dict) and payload.get("ok") is False:
        raise ToolExecutionError(str(payload.get("error") or "tool backend unavailable"), failure_type="connector_required")
    return content

def _tool_computer_use_real(args: Json) -> str:
    try:
        from . import gateway_computer_use as _cu
    except ImportError:
        import gateway_computer_use as _cu
    return _fail_if_json_tool_error(_cu._tool_computer_use(args))


def _tool_click_real(args: Json) -> str:
    try:
        from . import gateway_computer_use as _cu
    except ImportError:
        import gateway_computer_use as _cu
    return _fail_if_json_tool_error(_cu._tool_click(args))


def _tool_type_text_real(args: Json) -> str:
    try:
        from . import gateway_computer_use as _cu
    except ImportError:
        import gateway_computer_use as _cu
    return _fail_if_json_tool_error(_cu._tool_type_text(args))


def _tool_press_key_real(args: Json) -> str:
    try:
        from . import gateway_computer_use as _cu
    except ImportError:
        import gateway_computer_use as _cu
    return _fail_if_json_tool_error(_cu._tool_press_key(args))


def _tool_scroll_real(args: Json) -> str:
    try:
        from . import gateway_computer_use as _cu
    except ImportError:
        import gateway_computer_use as _cu
    return _fail_if_json_tool_error(_cu._tool_scroll(args))


def _tool_image_generation_real(args: Json) -> str:
    try:
        from . import gateway_computer_use as _cu
    except ImportError:
        import gateway_computer_use as _cu
    return _fail_if_json_tool_error(_cu._tool_image_generation(args))

def _build_builtin_tools() -> dict[str, GatewayTool]:
    path_props = {
        "file_path": {"type": "string"},
        "path": {"type": "string"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
    }
    tools = [
        GatewayTool(
            "echo_probe",
            "Return the input value. Used to verify real native tool calling.",
            _json_schema({"value": {"type": "string"}}, ["value"]),
            _tool_echo_probe,
            aliases=("gateway__echo_probe",),
        ),
        GatewayTool(
            "calculator",
            "Safely evaluate a basic arithmetic expression.",
            _json_schema({"expression": {"type": "string"}, "expr": {"type": "string"}}, ["expression"]),
            _tool_calculator,
            aliases=("calc", "gateway__calculator"),
        ),
        GatewayTool(
            "get_current_time",
            "Return the current time as ISO-8601.",
            _json_schema({"timezone": {"type": "string"}}),
            _tool_current_time,
            aliases=("gateway__get_current_time", "current_time"),
        ),
        GatewayTool("Read", "Read a text file from the workspace.", _json_schema(path_props), _tool_read, "read_local", aliases=("read_file", "FileReadTool", "view")),
        GatewayTool("ReadManyFiles", "Read multiple text files from the workspace.", _json_schema({"paths": {"type": "array"}, "max_files": {"type": "integer"}, "max_bytes_per_file": {"type": "integer"}}, ["paths"]), _tool_read_many_files, "read_local", aliases=("read_many_files", "read_files")),
        GatewayTool("FileInfo", "Return metadata for a workspace path.", _json_schema({"path": {"type": "string"}}, ["path"]), _tool_file_info, "read_local", aliases=("stat", "file_info")),
        GatewayTool("LS", "List a workspace directory.", _json_schema({"path": {"type": "string"}}), _tool_list_dir, "read_local", aliases=("list_dir",)),
        GatewayTool("Tree", "Render a bounded workspace directory tree.", _json_schema({"path": {"type": "string"}, "max_depth": {"type": "integer"}, "max_entries": {"type": "integer"}}), _tool_tree, "read_local", aliases=("tree", "directory_tree")),
        GatewayTool("CreateDirectory", "Create a workspace directory. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"path": {"type": "string"}, "parents": {"type": "boolean"}, "exist_ok": {"type": "boolean"}}, ["path"]), _tool_create_directory, "write_local", aliases=("mkdir", "create_directory")),
        GatewayTool("DeletePath", "Delete a workspace file or directory. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"path": {"type": "string"}, "recursive": {"type": "boolean"}}, ["path"]), _tool_delete_path, "write_local", aliases=("delete_file", "remove_file", "rm")),
        GatewayTool("MovePath", "Move or rename a workspace path. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"source": {"type": "string"}, "destination": {"type": "string"}, "overwrite": {"type": "boolean"}}, ["source", "destination"]), _tool_move_path, "write_local", aliases=("move_file", "rename_file", "mv")),
        GatewayTool("CopyPath", "Copy a workspace file. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"source": {"type": "string"}, "destination": {"type": "string"}, "overwrite": {"type": "boolean"}}, ["source", "destination"]), _tool_copy_path, "write_local", aliases=("copy_file", "cp")),
        GatewayTool("Glob", "Find files by glob pattern in the workspace.", _json_schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}}, ["pattern"]), _tool_glob, "read_local", aliases=("glob_files", "find_files")),
        GatewayTool("Grep", "Search workspace files with a regular expression.", _json_schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "limit": {"type": "integer"}}, ["pattern"]), _tool_grep, "read_local", aliases=("grep_files", "file_search")),
        GatewayTool("Write", "Write a file in the workspace. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"file_path": {"type": "string"}, "content": {"type": "string"}}, ["file_path", "content"]), _tool_write, "write_local", aliases=("write_file",)),
        GatewayTool("Edit", "Replace text in a workspace file. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, ["file_path", "old_string", "new_string"]), _tool_edit, "write_local", aliases=("edit_file",)),
        GatewayTool("MultiEdit", "Apply multiple string replacements to a workspace file. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"file_path": {"type": "string"}, "edits": {"type": "array"}}, ["file_path", "edits"]), _tool_multiedit, "write_local"),
        GatewayTool("RegexEdit", "Apply a regex replacement to a workspace file. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"file_path": {"type": "string"}, "pattern": {"type": "string"}, "replacement": {"type": "string"}, "replace_all": {"type": "boolean"}}, ["file_path", "pattern"]), _tool_regex_edit, "write_local", aliases=("regex_edit", "replace_regex")),
        GatewayTool("Bash", "Run a shell command in the workspace. Disabled unless GATEWAY_ALLOW_SHELL_TOOLS=1.", _json_schema({"command": {"type": "string"}, "cmd": {"type": "string"}, "cwd": {"type": "string"}, "workdir": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]), _tool_shell, "execute_code", aliases=("exec_command", "shell_command", "exec_shell", "shell", "local_shell", "user_shell")),
        GatewayTool("exec_shell_start", "Start an interactive shell command session. Disabled unless GATEWAY_ALLOW_SHELL_TOOLS=1.", _json_schema({"command": {"type": "string"}, "session_id": {"type": "string"}, "cwd": {"type": "string"}}, ["command"]), _tool_exec_shell_start, "execute_code", aliases=("exec_start", "shell_start")),
        GatewayTool("write_stdin", "Write stdin to an exec_shell_start session.", _json_schema({"session_id": {"type": "string"}, "chars": {"type": "string"}, "read_timeout": {"type": "number"}}, ["session_id"]), _tool_write_stdin, "execute_code", aliases=("exec_shell_interact", "exec_interact")),
        GatewayTool("exec_wait", "Wait for an exec_shell_start session.", _json_schema({"session_id": {"type": "string"}, "timeout": {"type": "number"}}, ["session_id"]), _tool_exec_wait, "execute_code", aliases=("exec_shell_wait", "BashOutput", "bash_output")),
        GatewayTool("exec_kill", "Terminate an exec_shell_start session.", _json_schema({"session_id": {"type": "string"}, "timeout": {"type": "number"}}, ["session_id"]), _tool_exec_kill, "execute_code", aliases=("kill_shell", "BashKill", "KillBash", "kill_bash")),
        GatewayTool("code_interpreter", "Run Python code locally and return stdout/stderr. Disabled unless GATEWAY_ALLOW_SHELL_TOOLS=1.", _json_schema({"code": {"type": "string"}, "description": {"type": "string"}, "language": {"type": "string"}, "timeout": {"type": "number"}, "cwd": {"type": "string"}}, ["code"]), _tool_code_interpreter, "execute_code", aliases=("python_interpreter", "python_exec")),
        GatewayTool("Git", "Run safe read-only git status/diff/log/show/branch commands.", _json_schema({"action": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}, "cached": {"type": "boolean"}}, ["action"]), _tool_git, "read_local", aliases=("git", "git_status", "git_diff", "git_log", "git_show")),
        GatewayTool("JsonQuery", "Query JSON data; file_path/path form is downstream workspace file access in cloud mode.", _json_schema({"data": {}, "file_path": {"type": "string"}, "query": {"type": "string"}}), _tool_json_query, "pure", aliases=("json_query", "jq")),
        GatewayTool("PythonSymbols", "Return Python class/function/import symbols for a file.", _json_schema({"file_path": {"type": "string"}, "path": {"type": "string"}}, ["file_path"]), _tool_python_symbols, "read_local", aliases=("python_symbols", "lsp_document_symbols")),
        GatewayTool("apply_patch", "Apply a Codex-style patch in the workspace. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"patch": {"type": "string"}}, ["patch"]), _tool_apply_patch, "write_local"),
        GatewayTool("WebFetch", "Fetch a URL over HTTP(S), including method/headers/body/json/form.", _json_schema({"url": {"type": "string"}, "method": {"type": "string"}, "headers": {"type": "object"}, "body": {"type": "string"}, "json": {}, "form": {"type": "object"}, "max_bytes": {"type": "integer"}}, ["url"]), _tool_fetch_url, "read_network", aliases=("fetch_url", "web_fetch", "fetch", "open")),
        GatewayTool("WebSearch", "Run a real web search via DuckDuckGo HTML and return parsed results.", _json_schema({"query": {"type": "string"}, "max_results": {"type": "integer"}, "search_url": {"type": "string"}}, ["query"]), _tool_web_search, "read_network", aliases=("web_search", "web_search_preview")),
        GatewayTool("TodoWrite", "Accept and persist a todo list in the conversation result.", _json_schema({"todos": {"type": "array"}}, ["todos"]), _tool_todo_write, "state", aliases=("todo_write",)),
        GatewayTool("update_plan", "Accept a plan/update_plan payload.", _json_schema({"plan": {"type": "array"}, "explanation": {"type": "string"}}), _tool_update_plan, "state"),
        GatewayTool("NotebookEdit", "Edit a Jupyter .ipynb notebook cell. Disabled unless GATEWAY_ALLOW_WRITE_TOOLS=1.", _json_schema({"notebook_path": {"type": "string"}, "cell_number": {"type": "integer"}, "cell_id": {"type": "string"}, "new_source": {"type": "string"}, "edit_mode": {"type": "string"}, "cell_type": {"type": "string"}}), _tool_notebook_edit, "write_local", aliases=("notebook_edit",)),
        GatewayTool("view_image", "Return local image metadata, dimensions, color summary, and optional base64 bytes.", _json_schema({"path": {"type": "string"}, "max_bytes": {"type": "integer"}, "histogram": {"type": "boolean"}}, ["path"]), _tool_view_image, "read_local", aliases=("ImageInfo", "AnalyzeImage", "image_info", "analyze_image", "inspect_image")),
        GatewayTool("list_mcp_resources", "List configured MCP resources through real MCP resources/list.", _json_schema({"server": {"type": "string"}}), _tool_list_mcp_resources, "mcp", aliases=("ListMcpResourcesTool",)),
        GatewayTool("list_mcp_resource_templates", "List configured MCP resource templates through real MCP resources/templates/list.", _json_schema({"server": {"type": "string"}}), _tool_list_mcp_resource_templates, "mcp"),
        GatewayTool("read_mcp_resource", "Read an MCP resource through real MCP resources/read.", _json_schema({"server": {"type": "string"}, "uri": {"type": "string"}}, ["uri"]), _tool_read_mcp_resource, "mcp", aliases=("mcp_read_resource", "ReadMcpResourceTool")),
        GatewayTool("mcp_get_prompt", "Fetch an MCP prompt through real MCP prompts/get.", _json_schema({"server": {"type": "string"}, "name": {"type": "string"}, "arguments": {"type": "object"}}, ["name"]), _tool_mcp_get_prompt, "mcp"),
        GatewayTool("mcp_list_tools", "List configured MCP server tools with gateway-compatible names.", _json_schema({"server": {"type": "string"}}), _tool_mcp_list_tools, "mcp", aliases=("list_mcp_tools", "McpListTools")),
        GatewayTool("mcp_call_tool", "Call a configured MCP tool by server/name or gateway public name.", _json_schema({"server": {"type": "string"}, "name": {"type": "string"}, "arguments": {"type": "object"}, "public_name": {"type": "string"}}), _tool_mcp_call_tool, "mcp", aliases=("call_mcp_tool", "McpCallTool")),
        GatewayTool("Memory", "Read/write compact Gateway SQLite memories.", _json_schema({"action": {"type": "string"}, "summary": {"type": "string"}, "keywords": {"type": "array"}, "limit": {"type": "integer"}}), _tool_memory, "state", aliases=("memory", "remember", "RecallMemory", "SaveMemory")),
        GatewayTool("multi_tool_use.parallel", "Execute multiple independent gateway tool calls and return their results.", _json_schema({"tool_uses": {"type": "array"}, "max_workers": {"type": "integer"}}, ["tool_uses"]), _tool_multi_tool_use_parallel, "orchestration", aliases=("parallel",)),
        GatewayTool("ExitPlanMode", "Record or echo a plan-mode result.", _json_schema({"plan": {"type": "string"}, "content": {"type": "string"}}), _tool_plan_mode, "state", aliases=("EnterPlanMode",)),
        GatewayTool("create_goal", "Accept and echo a Codex-style goal payload.", _json_schema({"goal": {"type": "string"}, "description": {"type": "string"}}), _tool_goal, "state", aliases=("update_goal",)),
        GatewayTool("IntentDetect", "Classify user text intent and suggest real Gateway tools to call next.", _json_schema({"text": {"type": "string"}, "input": {"type": "string"}, "query": {"type": "string"}, "prompt": {"type": "string"}}, ["text"]), _tool_intent_detect, "pure", aliases=("intent_detect", "intent_recognition", "TextIntent", "text_intent")),
        GatewayTool("Agent", "Run a real upstream AI subtask; large prompts are chunked and synthesized.", _json_schema({"prompt": {"type": "string"}, "description": {"type": "string"}, "files": {"type": "array"}, "max_workers": {"type": "integer"}}, ["prompt"]), _tool_agent, "ai_agent", aliases=("Task", "agent", "subagent")),
        GatewayTool("spawn_agent", "Start a real background Gateway Agent session and return an agent id.", _json_schema({"prompt": {"type": "string"}, "description": {"type": "string"}, "files": {"type": "array"}, "max_workers": {"type": "integer"}}), _tool_spawn_agent, "ai_agent"),
        GatewayTool("send_input", "Send follow-up input to a Gateway Agent session.", _json_schema({"target": {"type": "string"}, "message": {"type": "string"}}, ["target"]), _tool_send_input, "ai_agent"),
        GatewayTool("wait_agent", "Wait for one or more Gateway Agent sessions and return status/output.", _json_schema({"targets": {"type": "array"}, "timeout_ms": {"type": "integer"}}, ["targets"]), _tool_wait_agent, "ai_agent", aliases=("TaskOutput",)),
        GatewayTool("close_agent", "Close/cancel a Gateway Agent session.", _json_schema({"target": {"type": "string"}}, ["target"]), _tool_close_agent, "ai_agent"),
        GatewayTool("resume_agent", "Return current status for a Gateway Agent session.", _json_schema({"id": {"type": "string"}}, ["id"]), _tool_resume_agent, "ai_agent"),
        GatewayTool("request_user_input", "Record a structured user-input request as pending_user_input.", _json_schema({"questions": {"type": "array"}, "question": {"type": "string"}}), _tool_request_user_input, "state", aliases=("AskUserQuestion",)),
        GatewayTool("TeamCreate", "Create a lightweight local team/session mailbox.", _json_schema({"name": {"type": "string"}, "members": {"type": "array"}, "tasks": {"type": "array"}}), _tool_team_create, "state"),
        GatewayTool("SendMessage", "Send a message to a local team mailbox or Gateway Agent session.", _json_schema({"target": {"type": "string"}, "message": {"type": "string"}}, ["target"]), _tool_send_message, "state"),
        GatewayTool("TeamDelete", "Delete a local team/session mailbox.", _json_schema({"id": {"type": "string"}, "team_id": {"type": "string"}}), _tool_team_delete, "state"),
        GatewayTool("LSP", "Run lightweight local LSP-style actions such as document_symbols/search.", _json_schema({"action": {"type": "string"}, "file_path": {"type": "string"}, "pattern": {"type": "string"}}), _tool_lsp, "read_local"),
        GatewayTool("WebBrowser", "Fetch a web page as a lightweight browser-compatible action.", _json_schema({"url": {"type": "string"}}, ["url"]), _tool_web_browser, "read_network"),
        GatewayTool("file_search_call", "Search workspace files with a real local grep implementation.", _json_schema({"query": {"type": "string"}, "pattern": {"type": "string"}, "path": {"type": "string"}}), _tool_file_search_call, "read_local"),
        GatewayTool("web_search_call", "Run a real web search and return parsed results.", _json_schema({"query": {"type": "string"}, "max_results": {"type": "integer"}}), _tool_web_search_call, "read_network", aliases=("web_search_preview_2025_03_11",)),
        GatewayTool("Skill", "List/read local skills or execute a task with a skill guide through Agent.", _json_schema({"name": {"type": "string"}, "prompt": {"type": "string"}}), _tool_skill, "ai_agent", aliases=("skill", "list_skills", "read_skill", "run_skill")),
        # --- Real computer_use / GUI tools (no placeholders) ---
        GatewayTool("computer_use", "Take a screenshot of the current display. Returns path and optional base64.", _json_schema({"include_base64": {"type": "boolean"}}), _tool_computer_use_real, "gui", aliases=("computer_use_preview", "screenshot")),
        GatewayTool("computer_call", "Take a screenshot — alias for computer_use.", _json_schema({"include_base64": {"type": "boolean"}}), _tool_computer_use_real, "gui"),
        GatewayTool("image_generation", "Generate an image from a text prompt via Pollinations.ai (free) or configured API.", _json_schema({"prompt": {"type": "string"}, "size": {"type": "string"}}, ["prompt"]), _tool_image_generation_real, "read_network", aliases=("generate_image",)),
        GatewayTool("click", "Click at (x, y) on screen. Supports left/right/middle and double-click.", _json_schema({"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string"}, "double": {"type": "boolean"}}, ["x", "y"]), _tool_click_real, "gui", aliases=("mouse_click",)),
        GatewayTool("type_text", "Type a text string via real keyboard events.", _json_schema({"text": {"type": "string"}, "interval": {"type": "number"}}, ["text"]), _tool_type_text_real, "gui", aliases=("type_input", "keyboard_type")),
        GatewayTool("press_key", "Press a key or key combo (e.g. 'command+a', 'ctrl+shift+s', 'Enter').", _json_schema({"key": {"type": "string"}}, ["key"]), _tool_press_key_real, "gui", aliases=("key_press", "hotkey")),
        GatewayTool("scroll", "Scroll the mouse wheel. dx=horizontal, dy=vertical.", _json_schema({"dx": {"type": "integer"}, "dy": {"type": "integer"}, "x": {"type": "integer"}, "y": {"type": "integer"}}), _tool_scroll_real, "gui", aliases=("mouse_scroll",)),
    ]
    registry: dict[str, GatewayTool] = {}
    for tool in tools:
        registry[tool.name] = tool
        for alias in tool.aliases:
            registry[alias] = tool
    return registry

# Build the builtin tools registry
BUILTIN_TOOLS: dict[str, GatewayTool] = _build_builtin_tools()
