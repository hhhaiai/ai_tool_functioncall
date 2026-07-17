#!/usr/bin/env python3
"""Tool runtime for the gateway.

Handles tool call parsing, normalization, execution, and orchestration.
"""
from __future__ import annotations

import json
import copy
import logging
import os

_logger = logging.getLogger(__name__)
import pathlib
import re
import shlex
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from typing import Any

from .gateway_builtin_tools import (
    BUILTIN_TOOLS,
    ToolCall,
    ToolResult,
    _parse_json_arguments,
    _resolve_workspace_path,
    _response_text,
    _workspace_root,
)
from .gateway_config import (
    SUPPORTED_PATHS,
    _config_env,
    _configured_max_tool_rounds,
    _gateway_config,
    _upstream_config,
    load_config,
)
from .gateway_context import (
    _approx_token_count,
    _body_token_estimate,
    _context_config,
    _inject_recalled_memories,
    _maybe_compact_request_for_upstream,
    _remember_conversation_turn,
    _run_context_fanout,
)
from .gateway_agent_planner import (
    apply_synthesis_refusal_fallback as _agent_apply_synthesis_refusal_fallback,
    plan_downstream_tool_request as _agent_plan_downstream_tool_request,
    planner_intent_catalog as _agent_planner_intent_catalog,
    planner_session_key as _agent_planner_session_key,
    planner_state_snapshot as _agent_planner_state_snapshot,
    planner_workflow_catalog as _agent_planner_workflow_catalog,
    prepare_upstream_body as _agent_prepare_upstream_body,
    record_runtime_event as _agent_record_runtime_event,
    _session_key_index_parts as _agent_session_key_index_parts,
)
from .gateway_errors import BadRequestError, GatewayError, ToolExecutionError, UpstreamHTTPError
from .gateway_http_actions import _call_http_action, _enabled_http_actions, _http_action_by_name
from .gateway_logging import _record_tool_failure, _record_tool_stat
from .gateway_mcp import (
    _enabled_mcp_servers,
    _mcp_call_tool,
    _mcp_list_server_tools,
    _mcp_parse_public_name,
    _mcp_public_name,
    _mcp_server_by_name,
)
from .gateway_protocol import (
    _convert_request_to_upstream,
    _convert_response_to_downstream,
    _encode_tool_result_content,
    _forced_tool_name_from_choice,
    _from_openai_chat_response,
    _is_responses_tool_call_type,
    _last_user_text,
    _legacy_function_call_id,
    _replace_last_user_text,
    _responses_tool_call_arguments_value,
    _responses_tool_call_name,
    _without_tools,
)
from .gateway_proxy import NativeProxyClient
from .gateway_request_admission import _acquire_request_slot, _request_slot_scope
from .gateway_streaming import _merge_builtin_tools

Json = dict[str, Any]

DEFAULT_MAX_TOOL_ROUNDS = 5

# Concurrency control globals

# Path-like regex for cleaning text tool paths
_PATHISH_RE = re.compile(
    r"@?(?P<path>"
    r"(?:~?/|/|\.{1,2}/)[^\s<>'\"`|]+"
    r"|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.@%+=:,/-]+"
    r"|[A-Za-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|json|jsonl|toml|yaml|yml|md|txt|sh|bash|zsh|env|ini|cfg|conf|html|css|sql|go|rs|java|kt|swift|c|cc|cpp|h|hpp)"
    r")"
)


# =============================================================================
# Tool-runtime parsing and normalization utilities
# =============================================================================

def _first_present(args: Json, names: tuple[str, ...]) -> Any:
    """Return the first present (non-None) value from args for the given names."""
    for name in names:
        if name in args and args[name] is not None:
            return args[name]
    return None


def _clean_tool_string(value: Any) -> Any:
    """Clean tool string by stripping whitespace and XML-like tags."""
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    cdata = re.fullmatch(r"<!\[CDATA\[(.*)\]\]>", cleaned, flags=re.S)
    if cdata:
        cleaned = cdata.group(1).strip()
    cleaned = re.sub(r"</?(?:parameter|function|tool|tool_call|invoke)>", "", cleaned, flags=re.I).strip()
    return cleaned


def _clean_text_tool_path(value: Any) -> Any:
    """Extract a single path from noisy text-tool fallback parameters.

    Weak upstreams sometimes put prose after a path, e.g.
    ``README.md\n<tool_call>`` or ``src/app.py\n\n--- report``. Passing the
    whole blob to filesystem tools causes false not_found/File name too long
    failures, so path-like parameters are reduced to the first path token.
    """
    cleaned = _clean_tool_string(value)
    if not isinstance(cleaned, str):
        return cleaned
    text = cleaned.strip()
    if not text:
        return text
    for line in text.splitlines():
        candidate = line.strip().strip("`'\"")
        if not candidate:
            continue
        match = _PATHISH_RE.search(candidate)
        if match:
            return match.group("path").rstrip(".,;:)")
        if not re.match(r"^(?:[-*_]{3,}|#{1,6}\s|[*>]|\*\*)", candidate):
            return candidate.rstrip(".,;:)")
    return text


def _normalize_relative_pattern(value: Any) -> Any:
    """Normalize relative glob patterns to workspace-root relative."""
    value = _clean_tool_string(value)
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        return value.lstrip("/") or "*"
    return value


def _copy_model_override(body: Json) -> Json:
    """Copy body and override model with configured upstream model."""
    copied = dict(body)
    model = _config_env("UPSTREAM_MODEL", "")
    if model:
        copied["model"] = model
    return copied


def _has_requested_tools(body: Json) -> bool:
    """Check if the request body has tools defined."""
    tools = body.get("tools")
    return isinstance(tools, list) and bool(tools)


def _response_has_tool_calls(path: str, response: Json) -> bool:
    """Check if a response contains tool_calls in any protocol format."""
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message") or {}
            if isinstance(msg, dict) and (msg.get("tool_calls") or msg.get("function_call")):
                return True
        return False
    if path == "/v1/messages":
        for block in response.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        if response.get("stop_reason") == "tool_use":
            return True
        return False
    if path == "/v1/responses":
        for item in response.get("output") or []:
            if isinstance(item, dict) and _is_responses_tool_call_type(item.get("type")):
                return True
        return False
    return False


def _extract_openai_tool_calls_for_stream(response: Json) -> list[dict]:
    """Extract tool_calls from an OpenAI response formatted for SSE streaming chunks.
    Returns list of delta-style tool_call objects with index field."""
    result: list[dict] = []
    choice = (response.get("choices") or [{}])[0] if isinstance(response.get("choices"), list) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        return result
    tc_list = message.get("tool_calls")
    if isinstance(tc_list, list):
        for idx, tc in enumerate(tc_list):
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            result.append({
                "index": idx,
                "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": func.get("name") or "",
                    "arguments": func.get("arguments") or "{}",
                },
            })
    return result


def _fallback_response(path: str, text: str, *, status_note: str = "gateway_fallback") -> Json:
    """Generate a fallback response when upstream is unavailable."""
    model = _config_env("UPSTREAM_MODEL", "")
    output_tokens = _approx_token_count(text)
    usage = {"input_tokens": 0, "output_tokens": output_tokens, "total_tokens": output_tokens}
    if path == "/v1/messages":
        return {
            "id": f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
            "gateway_context": {"strategy": status_note},
        }
    if path == "/v1/responses":
        return {
            "id": f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "model": model,
            "status": "completed",
            "usage": usage,
            "gateway_context": {"strategy": status_note},
        }
    return {
        "id": f"chatcmpl_gateway_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
        "gateway_context": {"strategy": status_note},
    }


def _get_marketplace():
    """Lazy import for marketplace to avoid circular imports."""
    if not hasattr(_get_marketplace, '_cache'):
        try:
            from .marketplace import list_mcp_marketplace
            _get_marketplace._cache = list_mcp_marketplace
        except Exception:
            _get_marketplace._cache = lambda: []
    return _get_marketplace._cache


def _extract_client_project_dir(body: Json) -> pathlib.Path | None:
    """Detect the downstream client's project directory from request metadata.

    Claude Code injects session context into user/system blocks; Codex sends
    ``<environment_context><cwd>`` through Responses input.  Explicit metadata
    fields win first.  For natural-language/system-reminder text, prefer the
    latest matching path in the request because compacted summaries can contain
    stale ``Worktree:`` values from an older Gateway/service repo.
    """
    candidates: list[str] = []
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in (
            "gateway_workspace",
            "workspace_root",
            "project_dir",
            "projectDir",
            "workspace",
            "workspace_dir",
            "cwd",
            "working_directory",
            "primary_working_directory",
            "worktree",
        ):
            value = metadata.get(key)
            if isinstance(value, str):
                candidates.append(value)
        user_meta = metadata.get("user_id")
        if isinstance(user_meta, str):
            candidates.append(user_meta)
    elif isinstance(metadata, str):
        candidates.append(metadata)

    for key in (
        "project_dir",
        "projectDir",
        "workspace",
        "workspace_dir",
        "cwd",
        "working_directory",
        "primary_working_directory",
        "worktree",
    ):
        value = body.get(key)
        if isinstance(value, str):
            candidates.append(value)

    system = body.get("system")
    if isinstance(system, str):
        candidates.append(system)
    elif isinstance(system, list):
        for item in system:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    candidates.append(text)
            elif isinstance(item, str):
                candidates.append(item)

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        candidates.append(raw_input)
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    candidates.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            text = part.get("text") or part.get("input_text")
                            if isinstance(text, str):
                                candidates.append(text)
                        elif isinstance(part, str):
                            candidates.append(part)
                text = item.get("text") or item.get("input") or item.get("input_text")
                if isinstance(text, str):
                    candidates.append(text)

    messages = body.get("messages") or []
    message_texts: list[str] = []
    # Ordered by specificity - more specific patterns first
    patterns = [
        # Claude Code worktree pattern (highest priority - explicit isolation)
        re.compile(r"Worktree:\*?\*?\s*(/.+?)(?:\s*(?:\n|$))"),
        # Claude Code primary working directory pattern
        re.compile(r"Primary working directory:\*?\*?\s*(/.+?)(?:\s*(?:\n|$))"),
        # JSON projectDir pattern (handles both "projectDir": "/path" and projectDir: /path)
        # Also handles trailing quotes from JSON strings
        re.compile(r"""projectDir["']?\s*[:=]\s*["']?(/\S+?)["']?(?:\s|,|$|})"""),
        re.compile(r"""project[_-]?dir["']?\s*[:=]\s*["']?(/\S+?)["']?(?:\s|,|$|})""", re.I),
        re.compile(r"""workspace[_-]?root["']?\s*[:=]\s*["']?(/\S+?)["']?(?:\s|,|$|})""", re.I),
        re.compile(r"""gateway[_-]?workspace["']?\s*[:=]\s*["']?(/\S+?)["']?(?:\s|,|$|})""", re.I),
        # Codex CLI environment context.
        re.compile(r"<cwd>\s*(/.+?)\s*</cwd>", re.I | re.S),
        # Generic working directory pattern (lower priority)
        re.compile(r"Working directory:\*?\*?\s*(/.+?)(?:\s*(?:\n|$))"),
        # CWD pattern (last resort)
        re.compile(r"(?:^|\s)CWD:\s*(/\S+)"),
    ]

    def path_from_text(text: str) -> pathlib.Path | None:
        matches: list[tuple[int, int, str]] = []
        for priority, pat in enumerate(patterns):
            for match in pat.finditer(text):
                matches.append((match.start(1), -priority, match.group(1)))
        # In Claude Code prompts the live environment block is appended after
        # older summaries, so the later path is the safest source of truth.
        for _pos, _priority, raw_path in sorted(matches, reverse=True):
            cleaned = raw_path.strip().rstrip("\"'.,;:")
            # SECURITY FIX: Do NOT validate path existence on Gateway server
            # The path is on the CLIENT machine, not the Gateway server
            # Just validate it looks like a valid absolute path
            if cleaned.startswith('/') or cleaned.startswith('~'):
                try:
                    candidate = pathlib.Path(cleaned).expanduser()
                    # Return the path - it exists on client machine, not here
                    return candidate
                except (OSError, ValueError):
                    continue
        return None

    for raw in candidates:
        try:
            # SECURITY FIX: Do NOT validate path existence on Gateway server
            # The path is on the CLIENT machine, not the Gateway server
            # Just validate it looks like a valid path and return it
            cleaned = raw.strip().rstrip("\"'.,;:")
            if cleaned.startswith('/') or cleaned.startswith('~'):
                candidate = pathlib.Path(cleaned).expanduser()
                return candidate
        except (OSError, ValueError):
            pass
        path = path_from_text(raw)
        if path is not None:
            return path

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            message_texts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    message_texts.append(str(item.get("text") or ""))

    # Claude Code appends the live environment later in the prompt.  Previous
    # compacted summaries can contain stale Worktree values from another repo,
    # so scan user messages from newest text block to oldest and within each
    # block prefer the last match.
    for text in reversed(message_texts):
        path = path_from_text(text)
        if path is not None:
            return path
    return None


def _create_anonymous_workspace(body: Json) -> pathlib.Path:
    """Create an isolated anonymous workspace for a session.

    SECURITY: Each session gets its own isolated temporary directory.
    This prevents cross-session contamination and protects the Gateway server.

    The workspace is identified by:
    1. tenant/user + session_id from metadata
    2. random UUID fallback when the client did not provide identity

    Do not derive anonymous spaces from request text: on a remote service two
    users can send identical prompts, and they must never share a workspace.
    """
    import hashlib

    def stable_part(value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        text = str(text or "").strip()
        return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:24] if text else ""

    # Try to extract session_id and tenant/user from metadata
    session_id = None
    tenant_id = None
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        session_id = metadata.get("session_id") or metadata.get("conversation_id")
        tenant_id = metadata.get("tenant") or metadata.get("tenant_id") or metadata.get("account_id") or metadata.get("organization_id") or metadata.get("user")
        if not session_id:
            try:
                user_meta = json.loads(metadata.get("user_id") or "{}")
            except Exception:
                user_meta = {}
            if isinstance(user_meta, dict):
                session_id = user_meta.get("session_id") or user_meta.get("conversation_id")
                tenant_id = (
                    tenant_id
                    or user_meta.get("tenant")
                    or user_meta.get("tenant_id")
                    or user_meta.get("account_id")
                    or user_meta.get("organization_id")
                    or user_meta.get("user_id")
                    or user_meta.get("user")
                    or user_meta.get("email")
                )
        elif metadata.get("user_id"):
            tenant_id = tenant_id or metadata.get("user_id")

    # If no session_id, use a per-request random isolated space.  This is safer
    # for a remote multi-tenant service than hashing request content.
    if not session_id:
        session_id = f"request-{uuid.uuid4().hex}"

    tenant_id = tenant_id or body.get("client_id")
    tenant_part = stable_part(tenant_id) or "anonymous"
    session_part = stable_part(session_id)
    workspace_hint_part = stable_part(_logical_client_workspace_identifier(body))
    workspace_id = f"{tenant_part}-{session_part}"
    if workspace_hint_part:
        # A cloud client may send a logical workspace id (for example
        # ``workspace=project-a``) instead of a filesystem path.  Keep it as an
        # opaque namespace component; never resolve it against the Gateway cwd.
        workspace_id = f"{workspace_id}-{workspace_hint_part}"

    # Keep anonymous remote-client namespaces under the configured runtime
    # directory. Container deployments point this at their persistent data
    # volume; local installs retain the historical ~/.gateway_runtime default.
    runtime_dir = pathlib.Path(
        _config_env("GATEWAY_RUNTIME_DIR", str(pathlib.Path.home() / ".gateway_runtime"))
    )
    base_dir = runtime_dir / "anonymous_spaces"
    base_dir.mkdir(parents=True, exist_ok=True)

    workspace_dir = base_dir / workspace_id
    workspace_dir.mkdir(exist_ok=True)

    return workspace_dir.resolve()


def _log_workspace_resolution(source: str, path: pathlib.Path) -> None:
    """Log workspace resolution decision for debugging."""
    import logging
    import sys
    logger = logging.getLogger("gateway.workspace")
    # Always log workspace resolution for security auditing
    msg = f"✓ Workspace resolved via [{source}]: {path}"
    logger.info(msg)
    # Also print to stderr to ensure visibility
    print(msg, file=sys.stderr, flush=True)


def _request_workspace_root(body: Json) -> pathlib.Path:
    """Extract workspace root from request body.

    SECURITY: This function must NEVER return the Gateway server's working directory.
    All workspace paths MUST come from the client OR use an isolated anonymous space.

    Priority chain:
    1. Explicit body field (workspace_root or gateway_workspace)
    2. Auto-detected downstream project dir from session metadata
    3. Explicit env var (GATEWAY_WORKSPACE_ROOT) - for testing only
    4. Anonymous isolated space - per session/request temporary directory

    Returns a safe workspace path - never fails.
    """
    custom_root = body.get("workspace_root") or body.get("gateway_workspace") or body.get("workspace")
    if _is_absolute_client_workspace_value(custom_root):
        path = pathlib.Path(custom_root).expanduser().resolve()
        _log_workspace_resolution("explicit_body", path)
        return path
    # Auto-detect from Claude Code session metadata (Worktree / Primary working directory)
    detected = _extract_client_project_dir(body)
    if detected is not None:
        _log_workspace_resolution("session_metadata", detected)
        return detected
    if _has_non_absolute_client_workspace_hint(body):
        anonymous_space = _create_anonymous_workspace(body)
        _log_workspace_resolution("anonymous_space_relative_workspace", anonymous_space)
        return anonymous_space
    if _body_has_remote_identity(body):
        anonymous_space = _create_anonymous_workspace(body)
        _log_workspace_resolution("anonymous_space_remote_identity", anonymous_space)
        return anonymous_space
    # Only allow explicit env var for testing - not cwd
    env_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    if env_root:
        path = pathlib.Path(env_root).expanduser().resolve()
        _log_workspace_resolution("env_var", path)
        return path

    configured_root = str(_gateway_config().get("workspace_root") or "").strip()
    if configured_root:
        path = pathlib.Path(configured_root).expanduser().resolve(strict=False)
        _log_workspace_resolution("configured_root", path)
        return path

    # SECURITY: Create isolated anonymous space for this session
    # This allows users to chat even without providing workspace
    anonymous_space = _create_anonymous_workspace(body)
    _log_workspace_resolution("anonymous_space", anonymous_space)
    return anonymous_space


def _scoped_client_id(client_id: str) -> str:
    import hashlib

    text = str(client_id or "").strip()
    return f"client:{hashlib.sha256(text.encode('utf-8', 'ignore')).hexdigest()[:24]}"


def _request_scope_body(body: Json, client_id: str | None = None) -> Json:
    """Return a private body copy used only for Gateway runtime scoping.

    Authenticated cloud requests identify a downstream client through the
    validated API-key name passed as ``client_id``.  That identifier must be
    considered before workspace resolution so requests without explicit
    workspace metadata use an isolated anonymous remote scope instead of the
    Gateway service env/config root.  Keep this copy internal; do not send the
    synthetic, pseudonymous ``client_id`` field upstream.
    """
    if not str(client_id or "").strip() or not isinstance(body, dict):
        return body
    scoped = dict(body)
    scoped["client_id"] = _scoped_client_id(client_id)
    return scoped


def _is_absolute_client_workspace_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    return pathlib.Path(text).expanduser().is_absolute() or text.startswith("~")


_CLIENT_WORKSPACE_HINT_KEYS = (
    "workspace_root",
    "gateway_workspace",
    "workspace",
    "project_dir",
    "projectDir",
    "workspace_dir",
    "cwd",
    "working_directory",
    "primary_working_directory",
    "worktree",
)


def _direct_client_workspace_hint_values(body: Json) -> list[str]:
    values: list[str] = []
    for key in _CLIENT_WORKSPACE_HINT_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in _CLIENT_WORKSPACE_HINT_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value)
        user_meta = metadata.get("user_id")
        if isinstance(user_meta, str):
            try:
                parsed = json.loads(user_meta)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                for key in _CLIENT_WORKSPACE_HINT_KEYS:
                    value = parsed.get(key)
                    if isinstance(value, str) and value.strip():
                        values.append(value)
    return values


def _has_non_absolute_client_workspace_hint(body: Json) -> bool:
    return any(not _is_absolute_client_workspace_value(value) for value in _direct_client_workspace_hint_values(body))


def _logical_client_workspace_identifier(body: Json) -> str:
    for value in _direct_client_workspace_hint_values(body):
        if value.strip() and not _is_absolute_client_workspace_value(value):
            return value.strip()
    return ""


def _body_has_remote_identity(body: Json) -> bool:
    """Return true when a request identifies a remote tenant/session.

    In cloud mode, tenant/session metadata without a client workspace is still a
    real remote scope.  It must not fall back to the Gateway service
    GATEWAY_WORKSPACE_ROOT/config root, or unrelated clients can share runtime
    state under the service workspace.
    """
    metadata = body.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    metadata = metadata if isinstance(metadata, dict) else {}
    user_meta = metadata.get("user_id")
    if isinstance(user_meta, str):
        try:
            parsed_user = json.loads(user_meta)
        except Exception:
            parsed_user = {}
    elif isinstance(user_meta, dict):
        parsed_user = user_meta
    else:
        parsed_user = {}
    for container in (metadata, parsed_user, body):
        if not isinstance(container, dict):
            continue
        for key in (
            "tenant",
            "tenant_id",
            "account_id",
            "organization_id",
            "user",
            "user_id",
            "email",
            "session_id",
            "conversation_id",
            "thread_id",
            "client_id",
        ):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return True
    return False


def _text_has_absolute_workspace_hint(text: str) -> bool:
    return bool(re.search(
        r"(?:"
        r"<cwd>\s*[/~]|"
        r"Worktree:\s*[/~]|"
        r"Primary working directory:\s*[/~]|"
        r"(?:workspace[_-]?root|gateway[_-]?workspace|projectDir|project[_-]?dir|workspace[_-]?dir|cwd|working_directory)"
        r"[\"']?\s*[:=]\s*[\"']?[/~]"
        r")",
        str(text or ""),
        flags=re.I,
    ))


def _body_has_client_workspace_hint(body: Json) -> bool:
    """Return true when the request itself names the downstream workspace.

    Environment/config roots are Gateway service configuration.  They are useful
    for tests/admin-local proxy mode, but a cloud Gateway must not turn them
    into absolute file targets for downstream Codex/Claude Code tools.
    """
    for key in (
        "workspace_root",
        "gateway_workspace",
        "workspace",
        "project_dir",
        "projectDir",
        "workspace_dir",
        "cwd",
        "working_directory",
        "primary_working_directory",
        "worktree",
    ):
        if _is_absolute_client_workspace_value(body.get(key)):
            return True
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in (
            "gateway_workspace",
            "workspace_root",
            "project_dir",
            "projectDir",
            "workspace",
            "workspace_dir",
            "cwd",
            "working_directory",
            "primary_working_directory",
            "worktree",
        ):
            if _is_absolute_client_workspace_value(metadata.get(key)):
                return True
        user_meta = metadata.get("user_id")
        if isinstance(user_meta, str) and _text_has_absolute_workspace_hint(user_meta):
            return True
    elif isinstance(metadata, str) and _text_has_absolute_workspace_hint(metadata):
        return True
    return _extract_client_project_dir(body) is not None


def _downstream_declared_path_anchor(body: Json) -> pathlib.Path | None:
    if _gateway_executes_user_side_tools_locally() or _body_has_client_workspace_hint(body):
        try:
            return _workspace_root()
        except Exception:
            return None
    return None


def _stable_scope_part(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(text or "").strip()
    if not text:
        return ""
    if len(text) <= 96 and re.fullmatch(r"[A-Za-z0-9_.:@+-]+", text):
        return text
    return uuid.uuid5(uuid.NAMESPACE_URL, text).hex[:24]

def _request_runtime_scope_key(body: Json, root: pathlib.Path) -> str:
    """Build a multi-tenant namespace for process-global runtime state.

    The Gateway is a remote service.  Caller-visible ids such as
    ``session_id=dev`` or ``agent_id=worker`` are not globally unique, so
    server-side state must be namespaced by tenant/user + conversation +
    resolved client workspace.  Anonymous requests without a session get a
    per-request namespace, which is safer than sharing by prompt or cwd.
    """
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    user_meta_raw = metadata.get("user_id") if isinstance(metadata, dict) else None
    try:
        user_meta = json.loads(user_meta_raw) if isinstance(user_meta_raw, str) else (user_meta_raw if isinstance(user_meta_raw, dict) else {})
    except Exception:
        user_meta = {}
    tenant = (
        metadata.get("tenant")
        or metadata.get("tenant_id")
        or metadata.get("account_id")
        or metadata.get("organization_id")
        or user_meta.get("tenant")
        or user_meta.get("tenant_id")
        or user_meta.get("account_id")
        or user_meta.get("organization_id")
        or user_meta.get("user_id")
        or user_meta.get("user")
        or user_meta.get("email")
        or metadata.get("user")
        or body.get("user")
        or body.get("client_id")
        or "anonymous"
    )
    session = (
        metadata.get("session_id")
        or metadata.get("conversation_id")
        or user_meta.get("session_id")
        or user_meta.get("conversation_id")
        or body.get("session_id")
        or body.get("conversation_id")
        or ""
    )
    if not session:
        session = f"request-{uuid.uuid4().hex}"
    workspace = uuid.uuid5(uuid.NAMESPACE_URL, str(pathlib.Path(root).resolve())).hex[:24]
    return f"tenant:{_stable_scope_part(tenant)}:session:{_stable_scope_part(session)}:workspace:{workspace}"


def _agent_runtime_scope_from_request(path: str, body: Json) -> Json:
    """Return planner/runtime event scope for a remote request.

    Runtime events are service-side observability, but their keys must still be
    tenant + session + *client workspace* scoped.  This helper deliberately uses
    the same planner session-key builder as the Agent Planner so fallback
    dispatch events and Gateway-owned service-tool events line up with planner
    sessions in the admin APIs.
    """
    try:
        session_key = _agent_planner_session_key(path, body)
        parts = _agent_session_key_index_parts(session_key)
        return {
            "session_key": session_key,
            "tenant_key": str(parts.get("tenant_key") or ""),
            "workspace_key": str(parts.get("workspace_key") or ""),
        }
    except Exception:
        return {"session_key": "", "tenant_key": "", "workspace_key": ""}


def _record_agent_runtime_request_event(
    path: str,
    body: Json,
    *,
    event_type: str,
    workflow: str,
    step: str,
    summary: str,
    metadata: Json | None = None,
) -> None:
    try:
        scope = _agent_runtime_scope_from_request(path, body)
        _agent_record_runtime_event(
            session_key=str(scope.get("session_key") or ""),
            tenant_key=str(scope.get("tenant_key") or ""),
            workspace_key=str(scope.get("workspace_key") or ""),
            event_type=event_type,
            workflow=workflow,
            step=step,
            summary=summary,
            metadata=metadata or {},
        )
    except Exception:
        pass


def _tool_call_event_payload(call: ToolCall) -> Json:
    return {
        "id": call.call_id,
        "name": call.name,
        "arguments": call.arguments,
        "metadata": call.raw,
    }


def _bounded_tool_call_event_payload(call: ToolCall, *, max_chars: int = 1200) -> Json:
    """Return a bounded event payload for untrusted upstream tool attempts.

    Chat-only synthesis responses come from a weak upstream that has no tool
    authority. If it emits JSON/XML/function-call-looking text, the gateway
    records the attempt for debugging, but the runtime event must stay small and
    must not turn that content into an executable instruction.
    """
    payload = _tool_call_event_payload(call)
    try:
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        rendered = str(payload)
    if len(rendered) <= max_chars:
        return payload
    return {
        "id": call.call_id,
        "name": call.name,
        "arguments_preview": json.dumps(call.arguments, ensure_ascii=False)[:max_chars],
        "metadata_preview": json.dumps(call.raw, ensure_ascii=False)[:max_chars],
        "truncated": True,
    }


def _record_ignored_upstream_tool_attempt(
    path: str,
    body: Json,
    response: Json,
    *,
    source: str,
    scope_body: Json | None = None,
) -> None:
    """Record, but never execute, tool attempts emitted during chat-only synthesis."""
    if not _chat_only_synthesis_active(body):
        return
    native_calls = _extract_tool_calls(path, response)
    text_calls: list[ToolCall] = []
    if not native_calls:
        text_calls = _extract_text_tool_calls(path, response)
    calls = native_calls + text_calls
    if not calls:
        return
    response_text = _response_text(path, response) or ""
    _record_agent_runtime_request_event(
        path,
        scope_body or body,
        event_type="upstream_tool_attempt_ignored",
        workflow="chat_only_synthesis",
        step="ignore_upstream_tool_attempt",
        summary=f"ignored {len(calls)} upstream tool attempt(s) during chat-only synthesis",
        metadata={
            "source": source,
            "call_count": len(calls),
            "native_call_count": len(native_calls),
            "text_call_count": len(text_calls),
            "calls": [_bounded_tool_call_event_payload(call) for call in calls],
            "response_chars": len(response_text),
            "response_preview": response_text[:500],
            "tool_authority_granted": False,
        },
    )


def _chat_only_synthesis_body(body: Json) -> Json:
    """Remove tool surfaces before sending final synthesis to chat-only upstreams.

    In Agent Planner mode the upstream model is only a language synthesizer.
    Tool selection/execution already happened in the outer runtime (or was
    surfaced to the downstream client), so passing native tool schemas or text
    adapter manuals to a chat-only upstream reintroduces the old gateway/shim
    behavior and can make the weak model say "I'll call a tool" again.
    """
    cleaned = copy.deepcopy(body)
    cleaned.pop("tools", None)
    cleaned.pop("tool_choice", None)
    ctx = cleaned.setdefault("gateway_context", {})
    if isinstance(ctx, dict):
        ctx["chat_only_synthesis"] = True
        ctx["upstream_tools_stripped"] = True
    return cleaned


def _record_chat_only_synthesis_boundary_event(
    path: str,
    pre_body: Json,
    synthesis_body: Json,
    *,
    source: str,
    scope_body: Json | None = None,
) -> None:
    """Record that Agent Planner, not the upstream model, owns tool authority."""
    if not _chat_only_synthesis_active(synthesis_body):
        return
    ctx = synthesis_body.get("gateway_context") if isinstance(synthesis_body.get("gateway_context"), dict) else {}
    _record_agent_runtime_request_event(
        path,
        scope_body or pre_body,
        event_type="chat_only_synthesis_boundary",
        workflow="chat_only_synthesis",
        step="strip_upstream_tools",
        summary="Agent Planner stripped tool surfaces before chat-only upstream synthesis",
        metadata={
            "source": source,
            "had_tools": bool(pre_body.get("tools")),
            "had_tool_choice": pre_body.get("tool_choice") not in (None, "", "none"),
            "strategy": str(ctx.get("strategy") or ""),
            "upstream_tools_stripped": bool(ctx.get("upstream_tools_stripped")),
            "agent_planner_strict_every_turn": bool(ctx.get("agent_planner_strict_every_turn")),
            "planner_has_evidence": bool(ctx.get("planner_has_evidence")),
            "tool_authority_granted": False,
        },
    )


def _should_use_chat_only_synthesis_boundary(body: Json) -> bool:
    """Return true only for final turns already owned by the outer planner.

    Adapter/text fallback mode is also used by legacy orchestration tests and
    by upstreams that may still emit executable native/text tool calls.  The
    hard chat-only boundary must therefore be applied only after the Gateway
    Agent Planner (or a Gateway-owned service tool preexecute) has already
    produced the evidence that the upstream should synthesize from.
    """
    ctx = body.get("gateway_context") if isinstance(body, dict) else None
    if not isinstance(ctx, dict):
        return False
    if ctx.get("agent_planner_strict_every_turn"):
        return True
    if ctx.get("strategy") == "agent_planner_final_synthesis" and ctx.get("planner_has_evidence"):
        return True
    agent_ctx = ctx.get("agent_planner") if isinstance(ctx.get("agent_planner"), dict) else {}
    try:
        evidence_count = int(agent_ctx.get("evidence_count") or 0)
    except (TypeError, ValueError):
        evidence_count = 0
    if evidence_count > 0:
        return True
    # Gateway-owned service tools are pre-executed by the service before final
    # synthesis and must not give the upstream another chance to schedule tools.
    if str(agent_ctx.get("workflow") or "") == "gateway_owned_tool":
        return True
    return False


def _chat_only_synthesis_active(body: Json) -> bool:
    ctx = body.get("gateway_context") if isinstance(body, dict) else None
    return bool(isinstance(ctx, dict) and ctx.get("chat_only_synthesis"))


@contextmanager
def _workspace_scope(root: pathlib.Path, body: Json | None = None):
    """Context manager that temporarily changes the workspace root.

    SECURITY: root is always a valid path (client-provided or anonymous space).
    """
    from . import gateway_builtin_tools as _bt

    # Ensure the path is absolute and resolved
    resolved_root = pathlib.Path(root).resolve()

    _logger.debug("_workspace_scope: setting workspace to %s", resolved_root)

    token = _bt._WORKSPACE_ROOT_OVERRIDE.set(resolved_root)
    scope_token = None
    client_token = None
    if body is not None:
        scope_key = _request_runtime_scope_key(body, resolved_root)
        scope_token = _bt._RUNTIME_SCOPE_OVERRIDE.set(scope_key)
        client_id = body.get("client_id") if isinstance(body, dict) else None
        if isinstance(client_id, str) and client_id.strip():
            client_token = _bt._CLIENT_ID_SCOPE_OVERRIDE.set(client_id.strip())
        _logger.debug("_workspace_scope: setting runtime scope to %s", scope_key)
    try:
        yield resolved_root
    finally:
        if client_token is not None:
            _bt._CLIENT_ID_SCOPE_OVERRIDE.reset(client_token)
        if scope_token is not None:
            _bt._RUNTIME_SCOPE_OVERRIDE.reset(scope_token)
        _bt._WORKSPACE_ROOT_OVERRIDE.reset(token)
        _logger.debug("_workspace_scope: reset workspace")

def _normalize_tool_name(name: str) -> str:
    """Normalize tool name to match builtin registry."""
    if not name:
        return name
    # Direct match
    if name in BUILTIN_TOOLS:
        return name
    # Case-insensitive match
    lower = name.lower()
    for key in BUILTIN_TOOLS:
        if key.lower() == lower:
            return key
    # Strip common prefixes
    for prefix in ("gateway_", "gw_", "tool_"):
        if lower.startswith(prefix):
            stripped = name[len(prefix):]
            if stripped in BUILTIN_TOOLS:
                return stripped
    return name


def _normalize_tool_args(name: str, arguments: Json) -> Json:
    """Normalize tool arguments to match expected schema."""
    if not isinstance(arguments, dict):
        return arguments
    tool = BUILTIN_TOOLS.get(name)
    if not tool or not tool.parameters:
        return arguments
    props = tool.parameters.get("properties", {})
    if not props:
        return arguments
    # Map common aliases - only apply if the target property exists in schema
    alias_map = {
        "cmd": "command",
        "file": "path",
        "file_path": "path",
        "filepath": "path",
        "dir": "path",
        "directory": "path",
        "folder": "path",
        "input": "content",
        "text": "content",
        "data": "content",
        "value": "expression",
        "expr": "expression",
    }
    result = {}
    for key, value in arguments.items():
        mapped_key = alias_map.get(key, key)
        if mapped_key in props:
            result[mapped_key] = value
        else:
            result[key] = value
    if name == "Bash" and "command" in result and "cmd" in props and "cmd" not in result:
        result["cmd"] = result["command"]
    return result


def _normalize_tool_call(call: ToolCall) -> ToolCall:
    name = _normalize_tool_name(call.name)
    arguments = _normalize_tool_args(name, call.arguments)
    if name == "Git" and not arguments.get("action"):
        compact = re.sub(r"[^a-z0-9]+", "_", call.name.lower()).strip("_")
        for action in ("status", "diff", "log", "show", "branch"):
            if action in compact:
                arguments["action"] = action
                break
    return ToolCall(
        call_id=call.call_id,
        name=name,
        arguments=arguments,
        raw=call.raw,
    )


def _direct_tool_call_from_body(body: Json) -> ToolCall:
    raw: Json = body
    call_id = str(body.get("id") or body.get("call_id") or body.get("tool_call_id") or f"call_{uuid.uuid4().hex}")
    name: Any = body.get("name") or body.get("tool") or body.get("tool_name") or body.get("function_name") or body.get("recipient_name")
    if isinstance(name, str) and "." in name:
        name = name.rsplit(".", 1)[-1]
    raw_args: Any = body.get("arguments")
    if raw_args is None:
        raw_args = body.get("args")
    if raw_args is None:
        raw_args = body.get("input")
    if raw_args is None:
        raw_args = body.get("parameters")

    function = body.get("function")
    if isinstance(function, dict):
        name = name or function.get("name")
        raw_args = function.get("arguments") if raw_args is None else raw_args
        raw = function

    tool_call = body.get("tool_call")
    if isinstance(tool_call, dict):
        return _direct_tool_call_from_body(tool_call)

    if body.get("type") == "function" and isinstance(body.get("function"), dict):
        function = body["function"]
        name = function.get("name")
        raw_args = function.get("arguments")
        raw = body

    if body.get("type") == "tool_use":
        name = name or body.get("name")
        raw_args = body.get("input") if raw_args is None else raw_args
        raw = body

    if not name:
        raise ToolExecutionError("missing tool/function name", failure_type="invalid_input")
    return ToolCall(
        call_id=call_id,
        name=str(name),
        arguments=_parse_json_arguments(raw_args, allow_text=True),
        raw=raw,
    )


def _direct_tool_calls_from_body(body: Json) -> list[ToolCall]:
    if isinstance(body.get("tool_uses"), list):
        return [
            ToolCall(
                call_id=str(body.get("call_id") or body.get("id") or f"call_{uuid.uuid4().hex}"),
                name="multi_tool_use.parallel",
                arguments={"tool_uses": body.get("tool_uses"), "max_workers": body.get("max_workers")},
                raw=body,
            )
        ]
    raw_calls = body.get("tool_calls") or body.get("calls") or body.get("function_calls")
    if isinstance(raw_calls, list):
        return [_direct_tool_call_from_body(call) for call in raw_calls if isinstance(call, dict)]
    return [_direct_tool_call_from_body(body)]


def _response_tool_call_from_item(item: Json) -> ToolCall | None:
    item_type = item.get("type")
    if not _is_responses_tool_call_type(item_type):
        return None
    name = _responses_tool_call_name(item)
    if not name:
        return None
    return ToolCall(
        call_id=str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"),
        name=str(name),
        arguments=_responses_tool_call_arguments_value(item),
        raw=item,
    )


def _strip_xmlish_closing_tags(value: str) -> str:
    return re.sub(r"</(?:parameter|function|tool|invoke)>", "", value, flags=re.I).strip()



def _parse_parameter_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    # Stop at parameter, function, or tool_call tags
    parameter_re = re.compile(r"<parameter=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<parameter=[A-Za-z0-9_.:-]+>|<function=[A-Za-z0-9_.:-]+>|<tool_call>|\Z)", re.S)
    for param in parameter_re.finditer(text or ""):
        key = param.group(1).strip()
        value = _strip_xmlish_closing_tags(param.group(2))
        if key:
            blocks.append((key, value))
    return blocks


def _inline_text_before_parameter_blocks(text: str) -> str:
    raw = re.sub(r"<parameter=[A-Za-z0-9_.:-]+>.*", "", text or "", flags=re.S).strip()
    # Strip trailing junk after first blank line or markdown header
    raw = re.sub(r"\n\n.*", "", raw, flags=re.S).strip()
    # Strip trailing markdown headers
    raw = re.sub(r"\s*---.*", "", raw, flags=re.S).strip()
    return raw


def _repair_shell_command_spacing(command: str) -> str:
    """Repair common spacing loss from weak text-tool markup."""
    cmd = str(command or "").strip()
    if not cmd:
        return cmd
    cmd = re.sub(r"^(find)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"^(grep)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"^(ls|cat|head|tail|wc|python3?|bash|sh)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"\b(ls\s+-[A-Za-z]+)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"\s-type\s*([fdl])(?=\s|-|$)", r" -type \1", cmd)
    cmd = re.sub(r"(-type\s+[fdl])-name", r"\1 -name", cmd)
    cmd = re.sub(r"\s-name(')", r" -name \1", cmd)
    cmd = re.sub(r'\s-name(")', r' -name \1', cmd)
    cmd = re.sub(r"(?<!\s)-name(')", r" -name \1", cmd)
    cmd = re.sub(r'(?<!\s)-name(")', r' -name \1', cmd)
    cmd = re.sub(r'\s-name([^\s\'"]+)', r" -name \1", cmd)
    cmd = re.sub(r"\b(head|tail)-([0-9]+)\b", r"\1 -\2", cmd)
    cmd = re.sub(r"\b(wc\s+-[A-Za-z]+)\{\}", r"\1 {}", cmd)
    cmd = re.sub(r"\s-l\{\}", r" -l {}", cmd)
    cmd = re.sub(r"([^\s])\{\}(?=\s|$)", r"\1 {}", cmd)
    cmd = re.sub(r"\s+", " ", cmd).strip()
    return cmd


def _parse_json_tool_calls_from_text(text: str) -> list[ToolCall]:
    """Parse JSON-formatted tool calls from text responses.

    Supports: {"name": "X", "arguments": {...}}, {"tool": "X", "args": {...}},
    {"function": {"name": "X", "arguments": {...}}}, arrays thereof,
    and JSON inside ```json / ```functioncall code blocks.
    """
    if not text:
        return []
    calls: list[ToolCall] = []

    def _try(obj: dict) -> ToolCall | None:
        if not isinstance(obj, dict):
            return None
        name = obj.get("name") or obj.get("tool") or obj.get("function_name")
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or obj.get("input")
        if not name and isinstance(obj.get("function"), dict):
            fn = obj["function"]
            name = fn.get("name")
            args = args or fn.get("arguments")
        if not name and isinstance(obj.get("tool_calls"), list):
            for tc in obj["tool_calls"]:
                if isinstance(tc, dict):
                    fn = tc.get("function") or tc
                    if fn.get("name"):
                        name = fn["name"]
                        args = args or fn.get("arguments")
                        break
        if not name:
            return None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {"value": args}
        n = _normalize_tool_name(str(name))
        return ToolCall(
            call_id=f"textjson_{uuid.uuid4().hex}", name=n,
            arguments=_normalize_tool_args(n, args),
            raw={"gateway_text_tool_call_fallback": True, "format": "json", "text": text[:2000]},
        )

    # JSON in code blocks first
    code_block_re = re.compile(r"```(?:json|functioncall|tool_call|toolcall)?\s*\n(.*?)```", re.S | re.I)
    for m in code_block_re.finditer(text):
        block = m.group(1).strip()
        try:
            parsed = json.loads(block)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                c = _try(item)
                if c:
                    calls.append(c)
            if calls:
                return calls
        except json.JSONDecodeError:
            for line in block.splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "//")):
                    continue
                try:
                    c = _try(json.loads(line))
                    if c:
                        calls.append(c)
                except json.JSONDecodeError:
                    pass
    if calls:
        return calls

    # Raw JSON objects in text
    if '"name"' in text or '"tool"' in text:
        for m in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text):
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, dict) and (parsed.get("name") or parsed.get("tool") or parsed.get("function")):
                    c = _try(parsed)
                    if c:
                        calls.append(c)
            except json.JSONDecodeError:
                pass
    return calls


def _parse_markdown_tool_calls(text: str) -> list[ToolCall]:
    """Parse ```tool ...``` blocks and Python-style ToolName(key="value") calls."""
    if not text:
        return []
    calls: list[ToolCall] = []

    # ```tool ... ``` blocks
    for m in re.finditer(r"```(?:tool|tools|tool_call|toolcall)\s*\n(.*?)```", text, re.S | re.I):
        for line in m.group(1).strip().splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "//")):
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            name = parts[0]
            args: Json = {}
            if len(parts) > 1:
                for kv in re.finditer(r'(\w+)=(?:"([^"]*)"|\'([^\']*)\'|(\S+))', parts[1]):
                    args[kv.group(1)] = kv.group(2) or kv.group(3) or kv.group(4) or ""
                if not args:
                    args = {"input": parts[1]}
            n = _normalize_tool_name(name)
            calls.append(ToolCall(
                call_id=f"textmd_{uuid.uuid4().hex}", name=n,
                arguments=_normalize_tool_args(n, args),
                raw={"gateway_text_tool_call_fallback": True, "format": "markdown", "text": text[:2000]},
            ))
    if calls:
        return calls

    # Python-style: ToolName(key="value")
    for m in re.finditer(r'([A-Z][A-Za-z0-9_]*)\s*\(([^)]*)\)', text):
        name = m.group(1)
        n = _normalize_tool_name(name)
        if n not in BUILTIN_TOOLS and name not in BUILTIN_TOOLS:
            continue
        args: Json = {}
        for kv in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', m.group(2)):
            args[kv.group(1)] = kv.group(2) or kv.group(3) or kv.group(4) or ""
        calls.append(ToolCall(
            call_id=f"textpy_{uuid.uuid4().hex}", name=n,
            arguments=_normalize_tool_args(n, args),
            raw={"gateway_text_tool_call_fallback": True, "format": "python_call", "text": text[:2000]},
        ))
    return calls


def _parse_xml_tool_calls(text: str) -> list[ToolCall]:
    """Parse XML-format tool calls with function/parameter tags."""
    if "<function=" not in text and "<parameter=" not in text:
        return []
    calls: list[ToolCall] = []
    function_re = re.compile(r"<function=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<function=[A-Za-z0-9_.:-]+>|\Z)", re.S)

    def append_call(name: str, args: Json, raw_text: str) -> None:
        if not name:
            return
        n = _normalize_tool_name(name)
        calls.append(
            ToolCall(
                call_id=f"textcall_{uuid.uuid4().hex}",
                name=n,
                arguments=_normalize_tool_args(n, args),
                raw={"gateway_text_tool_call_fallback": True, "format": "xml", "text": raw_text[:2000]},
            )
        )

    matched_function = False
    for match in function_re.finditer(text):
        matched_function = True
        name = match.group(1).strip()
        body = match.group(2).strip()
        if body.startswith("{"):
            try:
                parsed = json.loads(_strip_xmlish_closing_tags(body))
                if isinstance(parsed, dict):
                    append_call(name, parsed, match.group(0))
                    continue
            except Exception:
                pass
        blocks = _parse_parameter_blocks(body)
        if name in {"Bash", "bash", "exec_command", "shell", "shell_command"}:
            inline_command = _inline_text_before_parameter_blocks(body)
            if inline_command:
                append_call(name, {"command": _repair_shell_command_spacing(inline_command)}, match.group(0))
            current: Json | None = None
            for key, value in blocks:
                if key in {"command", "cmd", "shell"}:
                    if current and current.get("command"):
                        append_call(name, current, match.group(0))
                    current = {"command": _repair_shell_command_spacing(value)}
                elif current is not None:
                    current[key] = value
            if current and current.get("command"):
                append_call(name, current, match.group(0))
            continue
        args: Json = {}
        for key, value in blocks:
            args[key] = value
        if not args:
            inline_value = _inline_text_before_parameter_blocks(body)
            normalized_name = _normalize_tool_name(name)
            if inline_value and normalized_name in {"Read", "FileInfo", "LS", "Tree", "Glob", "PythonSymbols", "JsonQuery"}:
                if normalized_name == "Glob":
                    args["pattern"] = inline_value
                else:
                    args["path"] = inline_value
        append_call(name, args, match.group(0))

    if not matched_function:
        current: Json | None = None
        for key, value in _parse_parameter_blocks(text):
            if key in {"command", "cmd", "shell"}:
                if current and current.get("command"):
                    append_call("Bash", current, text)
                current = {"command": _repair_shell_command_spacing(value)}
            elif current is not None:
                current[key] = value
        if current and current.get("command"):
            append_call("Bash", current, text)
    return calls


def _parse_text_tool_calls(text: str) -> list[ToolCall]:
    """Parse text-based tool-call fallbacks from weak upstream models.

    Supports multiple formats (tried in order):
    1. XML: function/parameter tags
    2. JSON: {"name": "ToolName", "arguments": {...}}
    3. Markdown: ```tool blocks or Python-style calls
    4. Bare parameter blocks
    """
    if not text:
        return []

    # Try XML format first
    calls = _parse_xml_tool_calls(text)
    if calls:
        return calls

    # Try JSON format
    calls = _parse_json_tool_calls_from_text(text)
    if calls:
        return calls

    # Try markdown/python-style format
    calls = _parse_markdown_tool_calls(text)
    if calls:
        return calls

    # Fallback: bare parameter blocks
    if "<parameter=" in text:
        current: Json | None = None
        for key, value in _parse_parameter_blocks(text):
            if key in {"command", "cmd", "shell"}:
                if current and current.get("command"):
                    calls.append(ToolCall(
                        call_id=f"textcall_{uuid.uuid4().hex}",
                        name="Bash",
                        arguments=_normalize_tool_args("Bash", current),
                        raw={"gateway_text_tool_call_fallback": True, "format": "bare_param", "text": text[:2000]},
                    ))
                current = {"command": _repair_shell_command_spacing(value)}
            elif current is not None:
                current[key] = value
        if current and current.get("command"):
            calls.append(ToolCall(
                call_id=f"textcall_{uuid.uuid4().hex}",
                name="Bash",
                arguments=_normalize_tool_args("Bash", current),
                raw={"gateway_text_tool_call_fallback": True, "format": "bare_param", "text": text[:2000]},
            ))

    return calls


def _extract_tool_calls(path: str, response: Json) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                calls.append(
                    ToolCall(
                        call_id=str(call.get("id") or f"call_{uuid.uuid4().hex}"),
                        name=str(fn["name"]),
                        arguments=_parse_json_arguments(fn.get("arguments")),
                        raw=call,
                    )
                )
            legacy_call = message.get("function_call")
            if isinstance(legacy_call, dict) and legacy_call.get("name"):
                calls.append(
                    ToolCall(
                        call_id=_legacy_function_call_id(legacy_call.get("name")),
                        name=str(legacy_call["name"]),
                        arguments=_parse_json_arguments(legacy_call.get("arguments")),
                        raw=legacy_call,
                    )
                )
        return calls

    if path == "/v1/responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            call = _response_tool_call_from_item(item)
            if call:
                calls.append(call)
            for block in item.get("content") or []:
                if isinstance(block, dict):
                    call = _response_tool_call_from_item(block)
                    if call:
                        calls.append(call)
        return calls

    if path == "/v1/messages":
        for block in response.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name"):
                calls.append(
                    ToolCall(
                        call_id=str(block.get("id") or f"toolu_{uuid.uuid4().hex}"),
                        name=str(block["name"]),
                        arguments=_parse_json_arguments(block.get("input") or {}),
                        raw=block,
                    )
                )
        return calls

    return calls


def _text_tool_call_fallback_enabled() -> bool:
    gateway = _gateway_config()
    upstream = _upstream_config()
    tools_enabled = str(upstream.get("tools_enabled", "adapter") or "adapter").strip().lower()
    if tools_enabled in {"off", "disabled", "false", "0", "none"}:
        return False
    capabilities = upstream.get("capabilities") if isinstance(upstream.get("capabilities"), dict) else {}
    native_capable = bool(capabilities.get("supports_tools", False)) and bool(capabilities.get("supports_function_calls", False))
    if tools_enabled in {"adapter", "text_only", "prompt"}:
        return True
    if tools_enabled == "auto" and not native_capable:
        return True
    return bool(gateway.get("text_tool_call_fallback_enabled", True))


def _extract_text_tool_calls(path: str, response: Json) -> list[ToolCall]:
    if not _text_tool_call_fallback_enabled():
        return []
    return _parse_text_tool_calls(_response_text(path, response))


def _convert_text_calls_to_downstream_response(
    path: str,
    text_calls: list["ToolCall"],
    original_response: Json,
    upstream_protocol: str,
) -> Json:
    """Convert parsed text-based tool calls into native downstream protocol format.

    Instead of executing tools locally, this creates a proper tool_use / function_call
    response that the downstream client (Claude Code / Codex) will execute.
    """
    if not text_calls:
        return original_response

    # Build Anthropic Messages format (tool_use blocks)
    if "/messages" in path:
        content_parts: list[dict] = []
        for call in text_calls:
            content_parts.append({
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": call.arguments,
            })
        return {
            "id": f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": original_response.get("model") or "",
            "content": content_parts,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": original_response.get("usage") or {"input_tokens": 0, "output_tokens": 0},
        }

    # Build OpenAI Chat format (tool_calls)
    if "/chat/completions" in path:
        tool_calls = []
        for call in text_calls:
            tool_calls.append({
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            })
        choices = original_response.get("choices") or [{}]
        choice = dict(choices[0]) if choices else {}
        message = dict(choice.get("message") or {})
        # Do not leak text-adapter markup to clients as visible assistant text.
        if isinstance(message.get("content"), str) and re.search(r"<function=|<tool_call>|```tool_code|\"tool_calls\"", message["content"]):
            message["content"] = None
        message["tool_calls"] = tool_calls
        choice["message"] = message
        choice["finish_reason"] = "tool_calls"
        return {
            "id": original_response.get("id") or f"chatcmpl_gateway_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "model": original_response.get("model") or "",
            "choices": [choice],
            "usage": original_response.get("usage") or {},
        }

    # Build OpenAI Responses format
    if "/responses" in path:
        output_items = []
        for call in text_calls:
            output_items.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            })
        return {
            "id": original_response.get("id") or f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "model": original_response.get("model") or "",
            "output": output_items,
            "usage": original_response.get("usage") or {},
        }

    return original_response


def _detect_intent_tool_calls(path: str, response: Json, body: Json) -> list[ToolCall]:
    """Detect tool usage intent from model response text for weak models.

    When a model can't generate proper tool calls but expresses intent to use
    tools (e.g., "I'll read the file", "Let me check the directory"), this
    function detects that intent and returns appropriate tool calls.

    This is a fallback for models that can't follow text-based tool call
    instructions.
    """
    _logger.debug("_detect_intent_tool_calls called")
    # Check if intent detection is enabled
    from .gateway_config import _gateway_config, _upstream_config
    gateway_cfg = _gateway_config()
    if not gateway_cfg.get("intent_detection_enabled", True):
        return []

    # Only enable for weak upstream models that can't generate tool calls.
    # Capabilities are stored under upstream.capabilities in the modern config;
    # keep the legacy top-level fallback for older local config files.
    upstream_cfg = _upstream_config()
    capabilities = upstream_cfg.get("capabilities") if isinstance(upstream_cfg.get("capabilities"), dict) else {}
    native_capable = (
        bool(capabilities.get("supports_tools", upstream_cfg.get("supports_tools", False)))
        and bool(capabilities.get("supports_function_calls", upstream_cfg.get("supports_function_calls", False)))
    )
    tools_enabled = str(upstream_cfg.get("tools_enabled", "adapter") or "adapter").strip().lower()
    if tools_enabled in {"off", "disabled", "false", "0", "none"}:
        return []
    if native_capable and tools_enabled not in {"off", "disabled", "false", "0", "none", "text_only", "adapter"}:
        return []  # Native tools supported, no need for intent detection

    text = _response_text(path, response)
    if not text:
        _logger.debug("Intent detection: no text in response")
        return []

    # Allow shorter responses for bare commands like "ls -la"
    text = text.strip()
    _logger.debug("Intent detection: response text = '%s'", text[:100])
    if len(text) < 3:
        _logger.debug("Intent detection: text too short (%d chars)", len(text))
        return []

    # Extract the last user message/input to understand context.  This supports
    # both Claude Code Anthropic Messages and Codex Responses payloads.
    _last_user_text(path, body)

    calls: list[ToolCall] = []

    # Pattern 0: Bare shell commands (highest priority)
    # Match standalone commands like "ls -la", "tree", "pwd", "find .", etc.
    # Strip trailing punctuation like . , ! ?
    clean_text = text.strip().rstrip(',!?;:')
    bare_cmd_pattern = r"^(ls|tree|pwd|find|grep|cat|head|tail|wc|du|df)(\s+.*)?$"
    bare_match = re.match(bare_cmd_pattern, clean_text, re.IGNORECASE)
    _logger.debug("Bare command pattern match on '%s': %s", clean_text, bare_match)
    if bare_match:
        cmd = clean_text
        _logger.debug("Detected bare command: '%s'", cmd)
        call = _shell_tool_call_for_downstream(
            body,
            cmd,
            {"gateway_intent_detection": True, "bare_command": True, "text": text[:500]},
        )
        if call is not None:
            calls.append(call)
        _logger.debug("Returning %d intent-detected tool calls", len(calls))
        return calls

    # Pattern 1: Model says it will read a file but doesn't output tool tags
    # Look for file paths mentioned in the response
    file_path_patterns = [
        # "read the file src/main.py" or "read src/main.py"
        r"(?:read|check|examine|look at|open|view)\s+(?:the\s+)?(?:file\s+)?(?:`([^`]+)`|(\S+\.\w+))",
        # "file at src/main.py" or "file: src/main.py"
        r"file\s+(?:at|:)\s*(?:`([^`]+)`|(\S+\.\w+))",
        # Backtick-quoted paths
        r"`([^`]+\.\w+)`",
    ]

    for pattern in file_path_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            file_path = match.group(1) or match.group(2) or match.group(3)
            if file_path and not file_path.startswith(("http://", "https://", "ftp://")):
                # Avoid duplicates
                if not any(c.arguments.get("path") == file_path for c in calls):
                    call = _declared_or_fallback_tool_call(
                        body,
                        f"intent_{uuid.uuid4().hex}",
                        ("Read", "read", "open", "view_file"),
                        "Read",
                        {"path": file_path},
                        {"gateway_intent_detection": True, "text": text[:500]},
                    )
                    if call is None:
                        call = _shell_tool_call_for_downstream(
                            body,
                            _read_shell_command(file_path),
                            {"gateway_intent_detection": True, "fallback_shell_tool": True, "text": text[:500]},
                        )
                    if call is not None:
                        calls.append(call)
                    break  # Only one Read per response
        if calls:
            break

    # Pattern 2: Model says it will list/check directory
    if not calls:
        dir_patterns = [
            r"\b(?:list|check|examine|look at|explore)\s+(?:the\s+)?(?:directory|folder|contents)\s+(?:of\s+)?(?:`([^`]+)`|(\S+))",
            r"\b(?:ls|dir)\b\s+(?:`([^`]+)`|(\S+))",
        ]
        for pattern in dir_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                dir_path = match.group(1) or match.group(2) or match.group(3) or "."
                call = _declared_or_fallback_tool_call(
                    body,
                    f"intent_{uuid.uuid4().hex}",
                    ("LS", "ls", "list", "list_files", "list_directory"),
                    "LS",
                    {"path": dir_path},
                    {"gateway_intent_detection": True, "text": text[:500]},
                )
                if call is None:
                    call = _shell_tool_call_for_downstream(
                        body,
                        f"ls -la {shlex.quote(dir_path)}",
                        {"gateway_intent_detection": True, "fallback_shell_tool": True, "text": text[:500]},
                    )
                if call is not None:
                    calls.append(call)
                break
            if calls:
                break

    # Pattern 3: Model says it will run a command
    if not calls:
        cmd_patterns = [
            r"(?:run|execute|use)\s+(?:the\s+)?(?:command|shell)\s*:?\s*`([^`]+)`",
            r"(?:run|execute)\s*:?\s*`([^`]+)`",
        ]
        for pattern in cmd_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                cmd = match.group(1)
                if cmd:
                    call = _shell_tool_call_for_downstream(
                        body,
                        cmd,
                        {"gateway_intent_detection": True, "text": text[:500]},
                    )
                    if call is not None:
                        calls.append(call)
                    break
            if calls:
                break

    # Pattern 4: Model says it will search/glob for files
    if not calls:
        glob_patterns = [
            r"(?:search|find|look for|glob)\s+(?:for\s+)?(?:files?\s+)?(?:matching\s+)?(?:`([^`]+)`|(\S+\.\w+))",
        ]
        for pattern in glob_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                glob_pattern = match.group(1) or match.group(2)
                if glob_pattern:
                    glob_name = _declared_tool_name_for_candidates(body, ("Glob", "glob", "file_search", "find_files"))
                    if glob_name is not None or not body.get("tools"):
                        glob_name = glob_name or "Glob"
                        calls.append(ToolCall(
                            call_id=f"intent_{uuid.uuid4().hex}",
                            name=glob_name,
                            arguments=_adapt_arguments_for_declared_tool(body, glob_name, {"pattern": glob_pattern}),
                            raw={"gateway_intent_detection": True, "text": text[:500]},
                        ))
                    else:
                        call = _shell_tool_call_for_downstream(
                            body,
                            f"find . -name {shlex.quote(glob_pattern)} | head -200",
                            {"gateway_intent_detection": True, "fallback_shell_tool": True, "text": text[:500]},
                        )
                        if call is not None:
                            calls.append(call)
                    break
            if calls:
                break

    # Pattern 5: After a downstream tool result (for example Skill loaded),
    # weak upstreams often answer with prose such as "Let me gather the project
    # structure" instead of a protocol tool call.  If the conversation still
    # contains the original project-inspection request, surface a real
    # downstream shell inspection call rather than ending with a placeholder.
    if not calls:
        response_lower = text.lower()
        wants_gather = any(token in response_lower for token in (
            "gather the project", "project structure", "key files", "analyze this project",
            "inspect the project", "读取项目", "项目结构", "关键文件", "分析项目",
        ))
        try:
            conversation_text = json.dumps(body.get("messages") or body.get("input") or body, ensure_ascii=False)
        except Exception:
            conversation_text = str(body)
        if wants_gather and _text_requests_project_inspection(conversation_text):
            call = _shell_tool_call_for_downstream(
                body,
                _project_inspection_shell_command("."),
                {"gateway_intent_detection": True, "intent": "project_inspection_followup", "fallback_shell_tool": True, "text": text[:500]},
            )
            if call is not None:
                calls.append(call)

    return calls


def _assistant_message_from_chat_response(response: Json) -> Json:
    choices = response.get("choices") or []
    if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict):
        return dict(choices[0]["message"])
    return {"role": "assistant", "content": None}


def _append_tool_results(path: str, body: Json, response: Json, results: list[ToolResult]) -> Json:
    updated = dict(body)
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        assistant_message = _assistant_message_from_chat_response(response)
        messages.append(assistant_message)
        legacy_function_call = (
            isinstance(assistant_message, dict)
            and isinstance(assistant_message.get("function_call"), dict)
            and not assistant_message.get("tool_calls")
        )
        for result in results:
            result_content = _encode_tool_result_content(result.content, not result.success)
            if legacy_function_call:
                messages.append({"role": "function", "name": result.name, "content": result_content})
            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "content": result_content,
                    }
                )
        updated["messages"] = messages
        return updated

    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        custom_call_ids: set[str] = set()
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                input_items.append(item)
                if item.get("type") == "custom_tool_call" and item.get("call_id"):
                    custom_call_ids.add(str(item["call_id"]))
            if isinstance(item, dict):
                for block in item.get("content") or []:
                    if isinstance(block, dict) and block.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                        input_items.append(block)
                        if block.get("type") == "custom_tool_call" and block.get("call_id"):
                            custom_call_ids.add(str(block["call_id"]))
        for result in results:
            output_type = "custom_tool_call_output" if result.call_id in custom_call_ids else "function_call_output"
            output_item = {
                "type": output_type,
                "call_id": result.call_id,
                "output": _encode_tool_result_content(result.content, not result.success),
            }
            if output_type == "custom_tool_call_output":
                output_item["name"] = result.name
            input_items.append(output_item)
        updated["input"] = input_items
        return updated

    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        content = response.get("content") or []
        messages.append({"role": "assistant", "content": content})
        result_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": result.call_id,
                "content": result.content,
                "is_error": not result.success,
            }
            for result in results
        ]
        messages.append({"role": "user", "content": result_blocks})
        updated["messages"] = messages
        return updated

    return updated


def _append_text_tool_results(path: str, body: Json, response: Json, calls: list[ToolCall], results: list[ToolResult]) -> Json:
    updated = dict(body)
    tool_report = {
        "gateway_local_tool_fallback": True,
        "reason": "upstream returned text-only <function=...> tool call markup without native protocol tool_calls/tool_use",
        "calls": [
            {
                "id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
                "success": result.success,
                "failure_type": result.failure_type,
                "content": result.content,
            }
            for call, result in zip(calls, results)
        ],
    }
    report_text = (
        "Gateway executed Gateway-owned or explicitly opted-in text-based tool calls. Real results below.\n"
        "Continue your analysis. If you need MORE tools, output them as:\n"
        "<function=ToolName><parameter=param>value</parameter></function>\n\n"
        + json.dumps(tool_report, ensure_ascii=False, indent=2)
    )
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        messages.append(_assistant_message_from_chat_response(response))
        messages.append({"role": "user", "content": report_text})
        updated["messages"] = messages
        return updated
    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        text = _response_text(path, response)
        if text:
            messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": report_text})
        updated["messages"] = messages
        return updated
    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        input_items.append({"role": "assistant", "content": _response_text(path, response)})
        input_items.append({"role": "user", "content": report_text})
        updated["input"] = input_items
        return updated
    return updated


def _extract_mentioned_paths(text: str) -> list[str]:
    candidates = re.findall(
        r"@?("
        r"/[^\s<>'\"`|]+"
        r"|(?:~|\.|\.\.)/[^\s<>'\"`|]+"
        r"|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.@%+=:,/-]+"
        r"|[A-Za-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|json|jsonl|toml|yaml|yml|md|txt|sh|bash|zsh|env|ini|cfg|conf|html|css|sql|go|rs|java|kt|swift|c|cc|cpp|h|hpp)"
        r")",
        text,
    )
    out: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().strip(".,;:，。；：）)]}\"'")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _should_build_local_planner_context(path: str, body: Json) -> bool:
    gateway = _gateway_config()
    if not gateway.get("local_planner_enabled", True):
        return False
    if path not in SUPPORTED_PATHS:
        return False
    text = _last_user_text(path, body)
    if not text:
        return False
    lowered = text.lower()
    read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开"))
    has_path = bool(_extract_mentioned_paths(text))
    if read_intent and has_path:
        return True
    analyze_intent = _text_requests_project_inspection(text)
    return analyze_intent


def _text_requests_project_inspection(text: str) -> bool:
    """Return True for prompts that require looking at the local code/workspace.

    This intentionally covers broad Chinese requests such as "分析这套项目" even
    when no explicit path is mentioned.  A chat-only upstream cannot see the
    user's filesystem by itself; surfacing a real downstream tool call is safer
    and more faithful than asking the upstream to guess from no evidence.
    """
    lowered = text.lower()
    project_tokens = (
        "这套项目", "这个项目", "当前项目", "本地项目", "项目结构",
        "工程", "代码", "代码库", "仓库", "目录结构", "这套代码", "这套工程",
        "this project", "current project", "local project", "codebase", "repo", "repository", "workspace",
    )
    inspect_tokens = (
        "分析", "审查", "检查", "梳理", "看看", "看一下", "了解", "理解", "解释", "总结",
        "analyze", "analyse", "review", "inspect", "investigate", "explain", "summarize",
    )
    explicit_phrases = (
        "分析代码", "分析这套项目", "分析这个项目", "分析当前项目", "分析本地项目",
        "项目结构", "代码分析", "代码审查", "梳理代码", "梳理架构",
        "analyze code", "analyze project", "analyze the project", "analyze this project",
        "review code", "code review", "inspect codebase", "explain this repo",
    )
    return (
        any(phrase in lowered for phrase in explicit_phrases)
        or (any(token in lowered for token in inspect_tokens) and any(token in lowered for token in project_tokens))
    )


def _text_requests_web_search(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in (
        "web search", "search web", "search online", "look up", "google", "browse",
        "搜索网页", "联网搜索", "上网查", "网上查", "查一下", "检索",
    ))


def _text_says_tool_work_is_needed(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in (
        "i will read", "i'll read", "let me read", "i need to read",
        "i will check", "i'll check", "let me check", "i need to check",
        "i will inspect", "let me inspect", "i need to inspect",
        "i will explore", "let me explore", "i need to explore",
        "我来读取", "我会读取", "需要读取", "先读取", "我来查看", "我会查看",
        "需要查看", "先查看", "我来检查", "需要检查", "我来分析文件",
    ))


def _extract_web_search_query(text: str) -> str:
    cleaned = re.sub(
        r"(?i)\b(?:please\s+)?(?:web\s+search|search\s+web|search\s+online|look\s+up|google|browse)\b[:：]?",
        "",
        text,
    )
    cleaned = re.sub(r"(?:请)?(?:搜索网页|联网搜索|上网查|网上查|查一下|检索)[:：]?", "", cleaned)
    cleaned = cleaned.strip().strip("`'\"“” \n\t")
    return cleaned[:500] or text.strip()[:500]


def _declared_tool_name_map_from_body(body: Json) -> dict[str, str]:
    """Map normalized/lowercase tool names to the exact caller-declared name."""
    names: dict[str, str] = {}
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        candidates: list[Any] = [tool.get("name")]
        function = tool.get("function")
        if isinstance(function, dict):
            candidates.append(function.get("name"))
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            exact = candidate.strip()
            for key in {exact, exact.lower(), _normalize_tool_name(exact), _normalize_tool_name(exact).lower()}:
                if key:
                    names.setdefault(key, exact)
    return names


def _preferred_declared_tool_name(body: Json, candidates: tuple[str, ...], fallback: str) -> str:
    declared = _declared_tool_name_map_from_body(body)
    for candidate in candidates:
        for key in (candidate, candidate.lower(), _normalize_tool_name(candidate), _normalize_tool_name(candidate).lower()):
            if key in declared:
                return declared[key]
    return fallback


def _declared_tool_name_for_candidates(body: Json, candidates: tuple[str, ...]) -> str | None:
    declared = _declared_tool_name_map_from_body(body)
    for candidate in candidates:
        for key in (candidate, candidate.lower(), _normalize_tool_name(candidate), _normalize_tool_name(candidate).lower()):
            if key in declared:
                return declared[key]
    return None


def _declared_gateway_builtin_name(body: Json, name: str) -> str | None:
    """Return the exact caller-declared name when it targets a Gateway builtin."""
    candidates: list[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    normalized = _normalize_tool_name(name)
    add(name)
    add(normalized)
    tool = BUILTIN_TOOLS.get(normalized) or BUILTIN_TOOLS.get(name)
    if tool is not None:
        add(tool.name)
        for registry_name, registry_tool in BUILTIN_TOOLS.items():
            if registry_tool is tool:
                add(registry_name)
    return _declared_tool_name_for_candidates(body, tuple(candidates))


def _declared_tool_shadows_gateway_builtin(body: Json, name: str) -> bool:
    """True when a caller-declared function name must override our builtin.

    In cloud Gateway mode, a request's private tool schema belongs to the
    downstream client even if its name collides with a pure/network Gateway
    helper such as ``calculator`` or ``WebSearch``.  Gateway-owned extension
    points (HTTP Actions/MCP public names) and explicit ``gateway__`` aliases
    remain service-owned.
    """
    declared_name = _declared_gateway_builtin_name(body, name)
    if not declared_name:
        return False
    if declared_name.lower().startswith("gateway__"):
        return False
    if _http_action_by_name(declared_name) or _mcp_parse_public_name(declared_name):
        return False
    tool = BUILTIN_TOOLS.get(_normalize_tool_name(name)) or BUILTIN_TOOLS.get(declared_name)
    return tool is not None and tool.risk not in USER_SIDE_TOOL_RISKS


def _declared_tool_specs_from_body(body: Json) -> list[tuple[str, str, Json]]:
    """Return exact caller-declared tool name, description, and input schema."""
    specs: list[tuple[str, str, Json]] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if isinstance(tool.get("function"), dict):
            fn = tool["function"]
            name = str(fn.get("name") or "").strip()
            description = str(fn.get("description") or tool.get("description") or "")
            schema = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        else:
            name = str(tool.get("name") or "").strip()
            description = str(tool.get("description") or "")
            if isinstance(tool.get("input_schema"), dict):
                schema = tool["input_schema"]
            elif isinstance(tool.get("parameters"), dict):
                # OpenAI Responses tools are top-level
                # {"type":"function","name":"...","parameters":{...}} rather
                # than chat-completions {"function":{...}}.
                schema = tool["parameters"]
            else:
                schema = {}
        if name:
            specs.append((name, description, schema))
    return specs


def _declared_tool_schema_from_body(body: Json, tool_name: str) -> Json:
    normalized = _normalize_tool_name(tool_name)
    for name, _description, schema in _declared_tool_specs_from_body(body):
        if name == tool_name or name.lower() == tool_name.lower() or _normalize_tool_name(name) == normalized:
            return schema
    return {}


def _schema_properties(schema: Json) -> dict[str, Any]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    return properties


def _schema_prefers_property(schema: Json, candidates: tuple[str, ...], fallback: str) -> str:
    properties = _schema_properties(schema)
    for candidate in candidates:
        if candidate in properties:
            return candidate
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    for candidate in candidates:
        if candidate in required:
            return candidate
    return fallback


def _adapt_arguments_for_declared_tool(body: Json, tool_name: str, arguments: Json) -> Json:
    """Adapt gateway semantic args to the exact downstream client schema.

    Codex Responses exposes ``exec_command(cmd=...)`` while Claude Code exposes
    ``Bash(command=...)`` and ``Read(file_path=...)``.  Returning gateway-native
    aliases such as ``Read(path=...)`` makes the real client reject the call
    before any local tool can run, so every synthesized downstream call must be
    reshaped against the caller-declared schema when one exists.
    """
    schema = _declared_tool_schema_from_body(body, tool_name)
    if not schema:
        return arguments
    adapted = dict(arguments)
    properties = _schema_properties(schema)
    normalized = _normalize_tool_name(tool_name).lower()

    if "command" in adapted:
        prop = _schema_prefers_property(schema, ("command", "cmd"), "command")
        if prop != "command":
            adapted[prop] = adapted.pop("command")
    if "path" in adapted:
        prop = _schema_prefers_property(schema, ("path", "file_path", "cwd", "directory"), "path")
        if prop != "path":
            adapted[prop] = adapted.pop("path")
        if prop == "file_path" and isinstance(adapted.get(prop), str):
            raw_path = adapted[prop]
            if raw_path and not pathlib.Path(raw_path).expanduser().is_absolute():
                root = _downstream_declared_path_anchor(body)
                if root is not None:
                    try:
                        adapted[prop] = str((root / raw_path).resolve(strict=False))
                    except Exception:
                        adapted[prop] = raw_path
    if "file_path" in adapted and isinstance(adapted.get("file_path"), str):
        raw_path = adapted["file_path"]
        if raw_path and not pathlib.Path(raw_path).expanduser().is_absolute():
            root = _downstream_declared_path_anchor(body)
            if root is not None:
                try:
                    adapted["file_path"] = str((root / raw_path).resolve(strict=False))
                except Exception:
                    adapted["file_path"] = raw_path
    if "name" in adapted and (normalized == "skill" or tool_name.lower() == "skill"):
        prop = _schema_prefers_property(schema, ("name", "skill"), "name")
        if prop != "name":
            adapted[prop] = adapted.pop("name")

    # Keep only schema-declared properties when the downstream schema is strict.
    if properties and schema.get("additionalProperties") is False:
        adapted = {key: value for key, value in adapted.items() if key in properties}
    return adapted


def _declared_or_fallback_tool_call(
    body: Json,
    call_id: str,
    candidates: tuple[str, ...],
    fallback: str,
    arguments: Json,
    raw: Json | None = None,
) -> ToolCall | None:
    declared_name = _declared_tool_name_for_candidates(body, candidates)
    if body.get("tools") and declared_name is None:
        return None
    name = declared_name or fallback
    return ToolCall(call_id, name, _adapt_arguments_for_declared_tool(body, name, arguments), raw or {"gateway_downstream_tool_request": True})


def _adapt_text_calls_for_declared_downstream_tools(body: Json, calls: list[ToolCall]) -> list[ToolCall]:
    """Reshape parsed text-tool calls to the exact caller-declared schema.

    Chat-only upstreams may emit fallback JSON such as
    ``{"name":"Edit","arguments":{"file_path":"..."}}``.  The text parser
    normalizes those against gateway builtin schemas, but the downstream client
    might have declared a different exact shape (Claude Code Read(file_path),
    Codex exec_command(cmd), etc.).  Before surfacing a text fallback as a
    native downstream tool request, re-bind the call to the declared tool name
    and argument schema.
    """
    if not calls:
        return calls
    adapted: list[ToolCall] = []
    for call in calls:
        candidates = (call.name, call.name.lower(), _normalize_tool_name(call.name), _normalize_tool_name(call.name).lower())
        declared = _declared_tool_name_for_candidates(body, candidates)
        name = declared or call.name
        adapted.append(ToolCall(
            call.call_id,
            name,
            _adapt_arguments_for_declared_tool(body, name, call.arguments),
            dict(call.raw or {}),
        ))
    return adapted


def _body_mentions_available_skill(body: Json, skill_name: str) -> bool:
    """Return True when the request context lists a skill as available."""
    try:
        haystack = json.dumps(body.get("messages") or body.get("input") or body, ensure_ascii=False)
    except Exception:
        haystack = str(body)
    return skill_name in haystack


def _shell_tool_call_for_downstream(body: Json, command: str, raw: Json | None = None) -> ToolCall | None:
    return _declared_or_fallback_tool_call(
        body,
        f"client_required_{uuid.uuid4().hex}",
        ("Bash", "bash", "shell", "exec_command"),
        "Bash",
        {"command": command},
        raw or {"gateway_downstream_tool_request": True, "fallback_shell_tool": True},
    )


def _read_shell_command(path: str) -> str:
    return f"python3 - <<'PY'\nfrom pathlib import Path\np = Path({path!r}).expanduser()\nprint(p.read_text(encoding='utf-8', errors='replace')[:20000], end='')\nPY"


def _project_inspection_shell_command(target: str = ".") -> str:
    quoted = shlex.quote(target)
    return (
        f"pwd; printf '\\n--- files ---\\n'; "
        f"find {quoted} -maxdepth 3 -type f "
        f"\\( -name '*.py' -o -name '*.md' -o -name '*.json' -o -name '*.toml' -o -name '*.yaml' -o -name '*.yml' \\) "
        f"| sed 's#^./##' | sort | head -200"
    )


_ARG_MISSING = object()


def _json_objects_from_text(text: str) -> list[Json]:
    candidates: list[Json] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.I | re.S):
        try:
            value = json.loads(match.group(1))
        except Exception:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _end = decoder.raw_decode(text[match.start():])
        except Exception:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    return candidates


def _declared_argument_object_from_text(user_text: str, properties: Json) -> Json:
    property_names = set(properties.keys())
    for obj in _json_objects_from_text(user_text):
        args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else obj
        if not isinstance(args, dict):
            continue
        if property_names and not (set(args.keys()) & property_names):
            continue
        return args
    return {}


def _schema_type(spec: Any) -> str:
    if not isinstance(spec, dict):
        return ""
    typ = spec.get("type")
    if isinstance(typ, list):
        for item in typ:
            if item != "null":
                return str(item)
        return ""
    return str(typ or "")


def _enum_value_from_text(text: str, spec: Any) -> Any:
    if not isinstance(spec, dict) or not isinstance(spec.get("enum"), list):
        return _ARG_MISSING
    lowered = text.lower()
    for item in spec["enum"]:
        item_text = str(item)
        if lowered == item_text.lower() or re.search(rf"(?<![A-Za-z0-9_]){re.escape(item_text.lower())}(?![A-Za-z0-9_])", lowered):
            return item
    return _ARG_MISSING


def _coerce_declared_argument_value(value: Any, spec: Any) -> Any:
    if not isinstance(spec, dict):
        return value
    enum_value = _enum_value_from_text(str(value), spec)
    if enum_value is not _ARG_MISSING:
        return enum_value
    typ = _schema_type(spec)
    if typ == "integer":
        if isinstance(value, bool):
            return _ARG_MISSING
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        match = re.search(r"-?\d+", str(value))
        return int(match.group(0)) if match else _ARG_MISSING
    if typ == "number":
        if isinstance(value, bool):
            return _ARG_MISSING
        if isinstance(value, (int, float)):
            return value
        match = re.search(r"-?(?:\d+\.\d+|\d+)", str(value))
        return float(match.group(0)) if match else _ARG_MISSING
    if typ == "boolean":
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"1", "true", "yes", "y", "on", "是", "开启", "启用"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", "否", "关闭", "禁用"}:
            return False
        return _ARG_MISSING
    if typ == "array":
        item_spec = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        raw_items = value if isinstance(value, list) else [part.strip() for part in re.split(r"[,，\n]+", str(value)) if part.strip()]
        coerced = []
        for item in raw_items:
            item_value = _coerce_declared_argument_value(item, item_spec)
            if item_value is _ARG_MISSING:
                return _ARG_MISSING
            coerced.append(item_value)
        return coerced
    if typ == "object":
        if isinstance(value, dict):
            nested_props = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}
            if not nested_props:
                return value
            out: Json = {}
            for key, nested_spec in nested_props.items():
                if key in value:
                    nested_value = _coerce_declared_argument_value(value[key], nested_spec)
                    if nested_value is not _ARG_MISSING:
                        out[str(key)] = nested_value
            if spec.get("additionalProperties") is not False:
                for key, nested_value in value.items():
                    out.setdefault(str(key), nested_value)
            return out
        return _ARG_MISSING
    if typ == "string":
        return str(value)
    return value


def _labelled_value_from_text(prop: str, user_text: str) -> str:
    match = re.search(rf"(?:{re.escape(prop)}|{re.escape(prop.replace('_', ' '))})\s*[:=]\s*([^\n;]+)", user_text, flags=re.I)
    return match.group(1).strip(" ,，。.!?") if match else ""


def _infer_declared_tool_arguments(name: str, schema: Json, user_text: str) -> Json | None:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    args: Json = {}
    explicit_args = _declared_argument_object_from_text(user_text, properties)
    if explicit_args and not properties:
        return explicit_args
    for prop, spec in properties.items():
        if prop in explicit_args:
            value = _coerce_declared_argument_value(explicit_args[prop], spec)
            if value is _ARG_MISSING:
                return None
            args[str(prop)] = value

    url_match = re.search(r"https?://[^\s`'\"<>]+", user_text)
    path_candidates = _extract_mentioned_paths(user_text)
    arithmetic_match = re.search(r"[-+*/().\d\s]{3,}", user_text)
    number_match = re.search(r"-?(?:\d+\.\d+|\d+)", user_text)
    location_match = re.search(
        r"(?:\bin\s+|\bfor\s+|天气[：:]?|weather\s+(?:in|for)\s+)([A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff ,.-]{1,80})",
        user_text,
        flags=re.I,
    )
    query = _extract_web_search_query(user_text) if _text_requests_web_search(user_text) else user_text.strip()

    def value_for(prop: str, spec: Any) -> Any:
        lower = prop.lower()
        enum_value = _enum_value_from_text(user_text, spec)
        if enum_value is not _ARG_MISSING:
            return enum_value
        labelled = _labelled_value_from_text(prop, user_text)
        if labelled:
            return _coerce_declared_argument_value(labelled, spec)
        if lower in {"query", "q", "search", "search_query", "keywords"}:
            return query
        if lower in {"location", "city", "place", "where"}:
            return (location_match.group(1).strip(" ?。.!") if location_match else user_text.strip())
        if lower in {"expression", "expr", "formula"}:
            return arithmetic_match.group(0).strip() if arithmetic_match else user_text.strip()
        if lower in {"url", "uri", "link"}:
            return url_match.group(0).rstrip(".,)") if url_match else user_text.strip()
        if lower in {"path", "file", "file_path", "filename"}:
            return path_candidates[-1] if path_candidates else user_text.strip()
        if lower in {"prompt", "input", "text", "message", "question"}:
            return user_text.strip()
        if isinstance(spec, dict):
            typ = _schema_type(spec)
            if typ in {"integer", "number"} and number_match:
                return _coerce_declared_argument_value(number_match.group(0), spec)
            if typ == "array":
                labelled_items = _labelled_value_from_text(prop, user_text)
                return _coerce_declared_argument_value(labelled_items, spec) if labelled_items else _ARG_MISSING
            if typ == "string":
                return user_text.strip()
            if typ == "boolean":
                lowered = user_text.lower()
                if any(token in lowered for token in (f"{lower} true", f"{lower}=true", f"{lower}: true", f"{lower} yes", f"{lower}=1")):
                    return True
                if any(token in lowered for token in (f"{lower} false", f"{lower}=false", f"{lower}: false", f"{lower} no", f"{lower}=0")):
                    return False
                return False
        return _ARG_MISSING

    for prop, spec in properties.items():
        if prop in args:
            continue
        if prop in required or prop.lower() in {"query", "q", "location", "city", "expression", "url", "path", "input", "text"}:
            value = value_for(str(prop), spec)
            if value is not _ARG_MISSING and value != "":
                args[str(prop)] = value
    for prop in required:
        if prop not in args:
            if isinstance(properties.get(prop), dict) and properties[prop].get("type") == "string":
                args[prop] = user_text.strip()
            else:
                return None
    return args


def _declared_function_tool_call_from_user_text(body: Json, user_text: str) -> ToolCall | None:
    """Deterministically select a caller-declared custom function when obvious.

    This is the stable outer-service path for chat-only upstreams: when a client
    supplies a single-purpose custom function such as ``get_weather`` and the
    user asks for weather, the gateway can return a real protocol-level tool
    call to the client instead of hoping the weak upstream invents one.
    """
    specs = []
    for name, description, schema in _declared_tool_specs_from_body(body):
        # Builtin/user-machine tools have dedicated safer planners above.
        shadows_gateway_builtin = _declared_tool_shadows_gateway_builtin(body, name)
        if _normalize_tool_name(name) in BUILTIN_TOOLS and not shadows_gateway_builtin:
            continue
        if _http_action_by_name(name) or _mcp_parse_public_name(name):
            continue
        specs.append((name, description, schema, shadows_gateway_builtin))
    if not specs:
        return None
    lowered = user_text.lower()
    best: tuple[int, str, str, Json] | None = None
    for name, description, schema, shadows_gateway_builtin in specs:
        normalized_name = name.lower().replace("_", " ").replace("-", " ")
        name_tokens = [tok for tok in re.split(r"[^a-z0-9\u4e00-\u9fff]+", normalized_name) if len(tok) >= 2]
        desc_tokens = [tok for tok in re.split(r"[^a-z0-9\u4e00-\u9fff]+", description.lower()) if len(tok) >= 4]
        score = 0
        if name.lower() in lowered or normalized_name in lowered:
            score += 4
        if shadows_gateway_builtin:
            tool = BUILTIN_TOOLS.get(_normalize_tool_name(name))
            canonical_name = tool.name if tool is not None else name
            if canonical_name == "calculator" and re.search(r"\d\s*[-+*/%]\s*\d", user_text):
                score += 5
            elif canonical_name == "get_current_time" and (
                any(token in lowered for token in ("current time", "what time", "time now", "now time"))
                or any(token in user_text for token in ("现在几点", "当前时间", "现在时间", "几点了"))
            ):
                score += 5
            elif canonical_name in {"WebSearch", "web_search_call"} and _text_requests_web_search(user_text):
                score += 5
        score += sum(1 for tok in name_tokens if tok in lowered)
        score += min(2, sum(1 for tok in desc_tokens if tok in lowered))
        if len(specs) == 1 and score > 0:
            score += 1
        if score and (best is None or score > best[0]):
            best = (score, name, description, schema)
    if best is None or best[0] < 2:
        return None
    _, name, _description, schema = best
    args = _infer_declared_tool_arguments(name, schema, user_text)
    if args is None:
        return None
    return ToolCall(
        f"client_required_{uuid.uuid4().hex}",
        name,
        args,
        {"gateway_downstream_tool_request": True, "declared_function_planner": True},
    )


def _gateway_owned_tool_calls_from_user_text(body: Json, user_text: str) -> list[ToolCall]:
    """Plan obvious Gateway-owned tool calls before involving chat-only upstreams.

    HTTP Actions and configured MCP connectors are service-owned capabilities:
    the weak upstream should not have to invent XML/JSON tool syntax for them.
    When the request clearly matches configured Gateway-owned actions, the outer
    planner can execute them and pass the results to the upstream only for final
    language synthesis.
    """
    specs: list[tuple[str, str, Json]] = []
    seen_names: set[str] = set()

    def add_spec(name: str, description: str, schema: Json) -> None:
        if not name or name in seen_names:
            return
        specs.append((name, description, schema if isinstance(schema, dict) else {}))
        seen_names.add(name)

    lowered = user_text.lower()
    arithmetic_requested = bool(
        re.search(r"\d\s*[-+*/%]\s*\d", user_text)
        and any(token in lowered for token in ("calculate", "calc", "math", "arithmetic", "compute", "what is", "多少", "计算", "算一下", "等于"))
    )
    time_requested = bool(
        any(token in lowered for token in ("current time", "what time", "time now", "now time"))
        or any(token in user_text for token in ("现在几点", "当前时间", "现在时间", "几点了"))
    )
    web_search_requested = _text_requests_web_search(user_text)

    # Built-in Gateway-owned capabilities are part of the service-side planner
    # registry too.  Keep this list intentionally narrow and capability-driven:
    # user-machine tools (filesystem/shell/GUI/local agents) must still be
    # surfaced to the downstream client, but pure/network service tools can run
    # before a chat-only upstream is asked to synthesize.
    for builtin_name, enabled in (
        ("calculator", arithmetic_requested),
        ("current_time", time_requested),
        ("WebSearch", web_search_requested),
    ):
        if not enabled:
            continue
        if _declared_tool_shadows_gateway_builtin(body, builtin_name):
            continue
        tool = BUILTIN_TOOLS.get(builtin_name)
        if tool is None or tool.risk in USER_SIDE_TOOL_RISKS:
            continue
        add_spec(tool.name, tool.description, tool.parameters)

    for name, description, schema in _declared_tool_specs_from_body(body):
        action = _http_action_by_name(name)
        if not action and not _mcp_parse_public_name(name):
            continue
        if action and (not schema.get("properties")) and isinstance(action.get("input_schema"), dict):
            schema = action["input_schema"]
        add_spec(name, description or str((action or {}).get("description") or ""), schema)
    # Configured HTTP Actions are Gateway-owned capabilities even when the
    # downstream client did not explicitly include a tools array.  This is the
    # agent-planner path: service-side capabilities are discovered from Gateway
    # config and executed before asking a chat-only upstream to synthesize.
    for action in _enabled_http_actions():
        name = str(action.get("name") or "").strip()
        if not name or name in seen_names:
            continue
        schema = action.get("input_schema") if isinstance(action.get("input_schema"), dict) else {}
        add_spec(name, str(action.get("description") or ""), schema)
    for server in _enabled_mcp_servers():
        server_name = str(server.get("name") or "").strip()
        if not server_name:
            continue
        try:
            for tool in _mcp_list_server_tools(server):
                tool_name = str(tool.get("name") or "").strip()
                if not tool_name:
                    continue
                public_name = _mcp_public_name(server_name, tool_name)
                if public_name in seen_names:
                    continue
                schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
                add_spec(public_name, str(tool.get("description") or f"MCP tool from {server_name}"), schema)
        except Exception:
            # MCP discovery is best-effort for planner preexecution.  The
            # normal explicit MCP tool path still reports detailed failures.
            continue
    if not specs:
        return []
    scored: list[tuple[int, int, str, Json]] = []
    for index, (name, description, schema) in enumerate(specs):
        normalized_name = name.lower().replace("_", " ").replace("-", " ")
        name_tokens = [tok for tok in re.split(r"[^a-z0-9\u4e00-\u9fff]+", normalized_name) if len(tok) >= 2]
        desc_tokens = [tok for tok in re.split(r"[^a-z0-9\u4e00-\u9fff]+", description.lower()) if len(tok) >= 4]
        score = 0
        if name.lower() in lowered or normalized_name in lowered:
            score += 4
        if name.lower() in {"calculator", "calc", "gateway__calculator"} and arithmetic_requested:
            score += 5
        if name.lower() in {"current_time", "get_current_time", "gateway__get_current_time"} and time_requested:
            score += 5
        if name.lower() in {"websearch", "web_search", "web_search_preview"} and web_search_requested:
            score += 5
        score += sum(1 for tok in name_tokens if tok in lowered)
        score += min(2, sum(1 for tok in desc_tokens if tok in lowered))
        if len(specs) == 1 and score > 0:
            score += 1
        if score >= 2:
            scored.append((score, index, name, schema))
    if not scored:
        return []
    calls: list[ToolCall] = []
    for _score, _index, name, schema in sorted(scored, key=lambda item: (-item[0], item[1])):
        args = _infer_declared_tool_arguments(name, schema, user_text)
        if args is None:
            continue
        calls.append(
            ToolCall(
                f"gateway_planner_{uuid.uuid4().hex}",
                name,
                args,
                {"gateway_agent_planner": True, "gateway_owned_preexecute": True},
            )
        )
    return calls


def _gateway_owned_tool_call_from_user_text(body: Json, user_text: str) -> ToolCall | None:
    calls = _gateway_owned_tool_calls_from_user_text(body, user_text)
    return calls[0] if calls else None


def _preexecute_gateway_owned_planner_tool(path: str, body: Json, client_id: str | None = None) -> Json:
    """Execute obvious Gateway-owned planner tools and append their evidence.

    This moves HTTP/MCP service tools into the outer planner path: chat-only
    upstreams receive the tool result as context and only synthesize the final
    answer.  User-machine tools remain downstream-owned and are not executed
    here.
    """
    if _has_tool_result_in_messages(path, body):
        return body
    user_text = _last_user_text(path, body)
    if not user_text:
        return body
    calls = [
        call
        for call in _gateway_owned_tool_calls_from_user_text(body, user_text)
        if not _tool_call_requires_downstream_execution(call, body)
    ]
    if not calls:
        return body
    results: list[ToolResult] = []
    for call in calls:
        _record_agent_runtime_request_event(
            path,
            body,
            event_type="gateway_tool_execute",
            workflow="gateway_owned_tool",
            step="preexecute_gateway_owned_tool",
            summary=f"execute Gateway-owned tool {call.name}",
            metadata={"call": _tool_call_event_payload(call), "client_id_present": bool(client_id)},
        )
        result = _execute_tool_call(call, client_id=client_id, provider="gateway_agent_planner")
        results.append(result)
        _record_agent_runtime_request_event(
            path,
            body,
            event_type="gateway_tool_result",
            workflow="gateway_owned_tool",
            step="preexecute_gateway_owned_tool",
            summary=f"Gateway-owned tool {call.name} {'succeeded' if result.success else 'failed'}",
            metadata={
                "call_id": call.call_id,
                "tool": call.name,
                "success": result.success,
                "failure_type": result.failure_type,
                "content_chars": len(result.content or ""),
            },
        )
    synthetic = _build_tool_round_response(
        path,
        calls,
        [],
        {"model": str(body.get("model") or _config_env("UPSTREAM_MODEL", "")), "usage": {"input_tokens": 0, "output_tokens": 0}},
    )
    updated = _append_tool_results(path, body, synthetic, results)
    # The selected Gateway-owned tool has already run.  Do not pass native tool
    # schemas or text-adapter manuals to the chat-only upstream for this final
    # synthesis turn.
    updated.pop("tools", None)
    updated.pop("tool_choice", None)
    ctx = updated.setdefault("gateway_context", {})
    if isinstance(ctx, dict):
        ctx["agent_planner"] = {
            "workflow": "gateway_owned_tool",
            "step": "preexecute_gateway_owned_tool",
            "tool": calls[0].name,
            "tools": [call.name for call in calls],
            "success": all(result.success for result in results),
        }
    return updated


def _extract_value_after_marker_request(text: str) -> str:
    """Return an explicit marker from "answer only the value after <marker>" prompts."""
    patterns = (
        r"(?:value|content|text)\s+after\s+[`'\"“”]?([A-Za-z0-9_.:-]+)",
        r"after\s+[`'\"“”]?([A-Za-z0-9_.:-]+)",
        r"[`'\"“”]([^`'\"“”\s]{3,})[`'\"“”]\s*(?:之后|后的值|后面的值)",
        r"([A-Za-z0-9_.:-]{3,})\s*(?:之后|后的值|后面的值)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            marker = match.group(1).strip().strip(".,;:，。；：）)]}\"'")
            if marker:
                return marker
    return ""


def _direct_local_file_read_response(path: str, body: Json) -> Json | None:
    """Satisfy narrow deterministic local-file read extraction prompts locally.

    Claude Code smoke tests and weak upstreams can ask to read a local file and
    output only the value after a marker.  If the active upstream does not emit a
    tool call, sending the prompt upstream can produce "I will read it" instead
    of the file value.  For explicit "value after <marker>" requests, the
    gateway can safely execute the read itself and return the exact value.
    """
    if not _gateway_executes_user_side_tools_locally():
        return None
    user_text = _last_user_text(path, body)
    if not user_text:
        return None
    lowered = user_text.lower()
    read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开"))
    marker = _extract_value_after_marker_request(user_text)
    paths = _extract_mentioned_paths(user_text)
    if not (read_intent and marker and paths):
        return None
    for raw_path in reversed(paths):
        try:
            resolved = _resolve_workspace_path(raw_path)
            if not resolved.is_file():
                continue
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        index = text.find(marker)
        if index < 0:
            continue
        value = text[index + len(marker):].lstrip(" \t:=：-")
        value = value.splitlines()[0].strip()
        if value and re.search(r"[A-Za-z0-9\u4e00-\u9fff]", value):
            return _fallback_response(path, value, status_note="gateway_local_file_read")
    return None


def _extract_explicit_skill_request(text: str) -> tuple[str, str]:
    """Return (action, skill_name) for explicit local Skill requests.

    This covers Claude Code/Codex prompts such as "list skills" or
    "read skill tdd" when the active upstream cannot reliably emit a structured
    Skill tool call.  The actual work still goes through the real Gateway Skill
    executor, so this is a deterministic local runtime shortcut, not a fake
    protocol-level tool result.
    """
    if not text:
        return "", ""
    lowered = text.lower()
    if not any(token in lowered for token in ("skill", "skills", "技能")):
        return "", ""
    if any(token in lowered for token in ("list skills", "show skills", "available skills", "列出", "有哪些", "技能列表", "所有技能", "可用技能")):
        return "list", ""
    read_patterns = (
        r"(?:read|show|open|view)\s+(?:the\s+)?skill\s+[`'\"“”]?([A-Za-z0-9_.-]+)",
        r"skill\s+[`'\"“”]?([A-Za-z0-9_.-]+)[`'\"“”]?\s*(?:内容|说明|指南|怎么用|是什么)",
        r"(?:读取|查看|打开|展示)\s*(?:skill|技能)\s*[`'\"“”]?([A-Za-z0-9_.-]+)",
    )
    for pattern in read_patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            name = match.group(1).strip().strip("`'\"“”.,;:，。；：")
            if name:
                return "read", name
    return "", ""


def _direct_local_skill_response(path: str, body: Json) -> Json | None:
    """Satisfy explicit local Skill list/read prompts for weak upstreams."""
    if not _gateway_executes_user_side_tools_locally():
        return None
    action, name = _extract_explicit_skill_request(_last_user_text(path, body))
    if not action:
        return None
    arguments = {"name": name} if action == "read" else {}
    result = _execute_tool_call(ToolCall(f"direct_skill_{uuid.uuid4().hex}", "Skill", arguments, {}), provider="direct_intent")
    if not result.success:
        return _fallback_response(path, result.content, status_note=f"gateway_local_skill_{result.failure_type or 'error'}")
    return _fallback_response(path, result.content, status_note=f"gateway_local_skill_{action}")


def _extract_explicit_shell_command_request(text: str) -> str:
    """Return a command only when the user explicitly asks Gateway to run one."""
    if not text:
        return ""
    lowered = text.lower()
    if not any(token in lowered for token in ("bash", "shell", "command", "run", "execute", "terminal", "命令", "运行", "执行")):
        return ""
    patterns = (
        r"(?:bash|shell|command|run|execute|terminal)[^`'\"]{0,120}`([^`\n]+)`",
        r"`([^`\n]+)`[^`]{0,120}(?:bash|shell|command|run|execute|terminal)",
        r"(?:命令|运行|执行)[^`'\"“”]{0,120}[`'\"“”]([^`'\"“”\n]+)[`'\"“”]",
        r"[`'\"“”]([^`'\"“”\n]+)[`'\"“”][^`'\"“”]{0,120}(?:命令|运行|执行)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            command = match.group(1).strip()
            if command:
                return command
    return ""


def _stdout_from_shell_tool_content(content: str) -> str:
    marker = "stdout:\n"
    if marker not in content:
        return ""
    stdout = content.split(marker, 1)[1]
    if "\nstderr:\n" in stdout:
        stdout = stdout.split("\nstderr:\n", 1)[0]
    return stdout.strip()


def _direct_local_bash_response(path: str, body: Json) -> Json | None:
    """Satisfy narrow explicit Bash/shell prompts locally for weak upstreams.

    This is not fake tool support: it only runs through the same permission-
    gated ``Bash`` runtime used by direct tool calls and text tool orchestration.
    It protects Claude Code/Codex adapter mode when a no-native-tools upstream
    merely says "I will run the command" instead of emitting adapter tags.
    """
    if not _gateway_executes_user_side_tools_locally():
        return None
    user_text = _last_user_text(path, body)
    command = _extract_explicit_shell_command_request(user_text)
    if not command:
        return None
    result = _execute_tool_call(ToolCall(f"direct_bash_{uuid.uuid4().hex}", "Bash", {"command": command}, {}), provider="direct_intent")
    if not result.success:
        return _fallback_response(path, result.content, status_note=f"gateway_local_bash_{result.failure_type or 'error'}")
    lowered = user_text.lower()
    stdout = _stdout_from_shell_tool_content(result.content)
    if stdout and any(token in lowered for token in ("stdout", "output only", "reply only", "answer only", "只输出", "仅输出")):
        return _fallback_response(path, stdout, status_note="gateway_local_bash")
    return _fallback_response(path, result.content, status_note="gateway_local_bash")


def _direct_downstream_tool_request_response(path: str, body: Json) -> Json | None:
    """Surface obvious user-machine tool requests without touching Gateway FS/shell."""
    if _gateway_executes_user_side_tools_locally():
        return None
    planner_decision = _agent_plan_downstream_tool_request(path, body)
    if planner_decision is not None and planner_decision.calls:
        response = _build_tool_round_response(
            path,
            planner_decision.calls,
            [],
            {"model": str(body.get("model") or _config_env("UPSTREAM_MODEL", "")), "usage": {"input_tokens": 0, "output_tokens": 0}},
        )
        ctx = response.setdefault("gateway_context", {})
        if isinstance(ctx, dict):
            state_snapshot = _agent_planner_state_snapshot(planner_decision.state)
            ctx["agent_planner"] = {
                "workflow": planner_decision.workflow,
                "step": planner_decision.step,
                "reason": planner_decision.reason,
                "session_key": planner_decision.state.get("session_key"),
                "intent": state_snapshot.get("intent") if isinstance(state_snapshot.get("intent"), dict) else {},
                "evidence_count": planner_decision.state.get("evidence_count", 0),
                "state": state_snapshot,
            }
        _record_agent_runtime_request_event(
            path,
            body,
            event_type="tool_dispatch",
            workflow="direct_downstream_tool_request",
            step="surface_user_side_tools",
            summary=f"surface {len(planner_decision.calls)} downstream user-machine tool request(s)",
            metadata={
                "calls": [_tool_call_event_payload(call) for call in planner_decision.calls],
                "source": "agent_planner",
                "owner": "downstream_client",
                "dispatch": "downstream_client",
                "planner_workflow": planner_decision.workflow,
                "planner_step": planner_decision.step,
            },
        )
        return response
    if _has_tool_result_in_messages(path, body):
        return None
    user_text = _last_user_text(path, body)
    if not user_text:
        return None
    calls: list[ToolCall] = []
    skill_action, skill_name = _extract_explicit_skill_request(user_text)
    if skill_action:
        arguments = {"name": skill_name} if skill_action == "read" else {}
        call = _declared_or_fallback_tool_call(
            body,
            f"client_required_{uuid.uuid4().hex}",
            ("Skill", "skill"),
            "Skill",
            arguments,
            {"gateway_downstream_tool_request": True},
        )
        if call is not None:
            calls.append(call)
    command = "" if calls else _extract_explicit_shell_command_request(user_text)
    if command:
        call = _shell_tool_call_for_downstream(body, command)
        if call is not None:
            calls.append(call)
    else:
        lowered = user_text.lower()
        paths = _extract_mentioned_paths(user_text)
        read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开"))
        list_intent = any(token in lowered for token in ("current directory", "list files", "list directory", "当前目录", "列出文件", "目录下", "ls "))
        web_intent = _text_requests_web_search(user_text)
        project_intent = _text_requests_project_inspection(user_text)
        if (
            project_intent
            and not paths
            and _declared_tool_name_for_candidates(body, ("Skill", "skill")) is not None
            and _body_mentions_available_skill(body, "codebase-onboarding")
        ):
            call = _declared_or_fallback_tool_call(
                body,
                f"client_required_{uuid.uuid4().hex}",
                ("Skill", "skill"),
                "Skill",
                {"name": "codebase-onboarding"},
                {"gateway_downstream_tool_request": True, "intent": "project_onboarding_skill"},
            )
            if call is not None:
                calls.append(call)
        elif read_intent and paths:
            # Prefer the path closest to the explicit read request. Claude Code
            # prompts often include stale Worktree/System-reminder paths before
            # the actual user request; asking the client to read all extracted
            # paths could leak or touch the wrong project.
            call = _declared_or_fallback_tool_call(
                body,
                f"client_required_{uuid.uuid4().hex}",
                ("Read", "read", "open", "view_file"),
                "Read",
                {"path": paths[-1]},
                {"gateway_downstream_tool_request": True},
            )
            if call is None:
                call = _shell_tool_call_for_downstream(body, _read_shell_command(paths[-1]))
            if call is not None:
                calls.append(call)
        elif list_intent:
            target = paths[-1] if paths else "."
            call = _declared_or_fallback_tool_call(
                body,
                f"client_required_{uuid.uuid4().hex}",
                ("LS", "ls", "list", "list_files", "list_directory"),
                "LS",
                {"path": target},
                {"gateway_downstream_tool_request": True},
            )
            if call is None:
                call = _shell_tool_call_for_downstream(body, f"ls -la {shlex.quote(target)}")
            if call is not None:
                calls.append(call)
        elif web_intent:
            search_name = _declared_tool_name_for_candidates(
                body,
                ("web_search", "WebSearch", "web_search_preview", "search", "google", "browser_search"),
            )
            if search_name is not None or not body.get("tools"):
                search_name = search_name or "WebSearch"
                calls.append(ToolCall(
                    f"client_required_{uuid.uuid4().hex}",
                    search_name,
                    _adapt_arguments_for_declared_tool(body, search_name, {"query": _extract_web_search_query(user_text)}),
                    {"gateway_downstream_tool_request": True},
                ))
        else:
            declared = _declared_tool_name_map_from_body(body)
            has_declared_workspace_tools = any(
                key in declared
                for key in ("LS", "ls", "Glob", "glob", "Read", "read", "Bash", "bash", "exec_command")
            )
            project_can_surface = (not body.get("tools")) or has_declared_workspace_tools
        if not calls and project_intent and not paths and len(user_text) < 2000 and project_can_surface:
            # A chat-only upstream cannot inspect local files.  Surface a small,
            # safe, protocol-level first tool fanout to the downstream client
            # (Codex/Claude Code) so it can provide real local evidence.
            target = paths[-1] if paths else "."
            ls_call = _declared_or_fallback_tool_call(
                body,
                f"client_required_{uuid.uuid4().hex}",
                ("LS", "ls", "list", "list_files", "list_directory"),
                "LS",
                {"path": target},
                {"gateway_downstream_tool_request": True, "intent": "project_inspection"},
            )
            glob_name = _declared_tool_name_for_candidates(body, ("Glob", "glob", "file_search", "find_files"))
            if ls_call is not None:
                calls.append(ls_call)
            if glob_name is not None or not body.get("tools"):
                glob_name = glob_name or "Glob"
                calls.append(ToolCall(
                    f"client_required_{uuid.uuid4().hex}",
                    glob_name,
                    _adapt_arguments_for_declared_tool(body, glob_name, {"pattern": "**/*.py", "path": target}),
                    {"gateway_downstream_tool_request": True, "intent": "project_inspection"},
                ))
                calls.append(ToolCall(
                    f"client_required_{uuid.uuid4().hex}",
                    glob_name,
                    _adapt_arguments_for_declared_tool(body, glob_name, {"pattern": "**/*.md", "path": target}),
                    {"gateway_downstream_tool_request": True, "intent": "project_inspection"},
                ))
            if not calls:
                call = _shell_tool_call_for_downstream(
                    body,
                    _project_inspection_shell_command(target),
                    {"gateway_downstream_tool_request": True, "intent": "project_inspection", "fallback_shell_tool": True},
                )
                if call is not None:
                    calls.append(call)
        elif not calls:
            declared_call = _declared_function_tool_call_from_user_text(body, user_text)
            if declared_call is not None:
                calls.append(declared_call)
    if not calls:
        return None
    _record_agent_runtime_request_event(
        path,
        body,
        event_type="tool_dispatch",
        workflow="direct_downstream_tool_request",
        step="surface_user_side_tools",
        summary=f"surface {len(calls)} downstream user-machine tool request(s)",
        metadata={
            "calls": [_tool_call_event_payload(call) for call in calls],
            "source": "fallback_intent",
            "owner": "downstream_client",
            "dispatch": "downstream_client",
        },
    )
    response = _build_tool_round_response(
        path,
        calls,
        [],
        {"model": str(body.get("model") or _config_env("UPSTREAM_MODEL", "")), "usage": {"input_tokens": 0, "output_tokens": 0}},
    )
    ctx = response.setdefault("gateway_context", {})
    if isinstance(ctx, dict):
        declared_function = any(
            isinstance(call.raw, dict) and call.raw.get("declared_function_planner")
            for call in calls
        )
        workflow = "project_analysis" if _text_requests_project_inspection(user_text) else "direct_downstream_tool_request"
        step = "custom_function" if declared_function else ("project_structure" if workflow == "project_analysis" else "surface_user_side_tools")
        ctx.setdefault("agent_planner", {
            "workflow": workflow,
            "step": step,
            "reason": "fallback downstream fanout from planner-classified user intent",
            "session_key": _agent_planner_session_key(path, body),
            "intent": {
                "kind": "project_analysis" if workflow == "project_analysis" else "tool_request",
                "workflow": workflow,
                "source": "current_user_text",
            },
            "evidence_count": 0,
            "state": {
                "workflow": workflow,
                "current_step": step,
                "session_key": _agent_planner_session_key(path, body),
            },
        })
    return response


def _weak_upstream_text_tools_active(gateway_mode: str) -> bool:
    """Return True when the gateway must compensate for non-native tool support."""
    if gateway_mode in {"passthrough", "native_passthrough", "proxy"}:
        return False
    upstream = _upstream_config()
    tools_enabled = str(upstream.get("tools_enabled", "adapter") or "adapter").strip().lower()
    capabilities = upstream.get("capabilities") if isinstance(upstream.get("capabilities"), dict) else {}
    native_capable = bool(capabilities.get("supports_tools", False)) and bool(capabilities.get("supports_function_calls", False))
    if tools_enabled in {"text_only", "adapter", "prompt"}:
        return True
    if tools_enabled == "auto" and not native_capable:
        return True
    return False


USER_SIDE_TOOL_RISKS = {"read_local", "write_local", "execute_code", "gui", "ai_agent"}


def planner_capability_catalog(*, include_mcp_tools: bool = False) -> Json:
    """Return a bounded Agent Planner capability registry snapshot.

    This is an observability surface, not an execution path.  It makes the
    runtime's ownership model explicit for remote operators: service-owned
    capabilities may run inside the Gateway, while user-machine capabilities
    must be surfaced to the downstream client workspace.
    """

    def _schema_summary(schema: Json) -> Json:
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        return {
            "properties": sorted(str(name) for name in props.keys())[:50],
            "required": [str(name) for name in required][:50],
        }

    def _tool_entry(name: str, description: str, schema: Json, *, owner: str, source: str, risk: str = "") -> Json:
        return {
            "name": name,
            "owner": owner,
            "source": source,
            "risk": risk,
            "description": (description or "")[:300],
            "schema": _schema_summary(schema if isinstance(schema, dict) else {}),
        }

    service_side: list[Json] = []
    downstream_owned: list[Json] = []
    seen_service: set[str] = set()
    seen_downstream: set[str] = set()

    for tool in BUILTIN_TOOLS.values():
        if tool.risk in USER_SIDE_TOOL_RISKS:
            if tool.name not in seen_downstream:
                downstream_owned.append(
                    _tool_entry(
                        tool.name,
                        tool.description,
                        tool.parameters,
                        owner="downstream_client",
                        source="builtin",
                        risk=tool.risk,
                    )
                )
                seen_downstream.add(tool.name)
        else:
            if tool.name not in seen_service:
                service_side.append(
                    _tool_entry(
                        tool.name,
                        tool.description,
                        tool.parameters,
                        owner="gateway_service",
                        source="builtin",
                        risk=tool.risk,
                    )
                )
                seen_service.add(tool.name)

    http_actions: list[Json] = []
    for action in _enabled_http_actions():
        name = str(action.get("name") or "").strip()
        if not name:
            continue
        schema = action.get("input_schema") if isinstance(action.get("input_schema"), dict) else {}
        entry = _tool_entry(
            name,
            str(action.get("description") or ""),
            schema,
            owner="gateway_service",
            source="http_action",
            risk="network",
        )
        entry["method"] = str(action.get("method") or "GET").upper()
        http_actions.append(entry)
        if name not in seen_service:
            service_side.append(entry)
            seen_service.add(name)

    mcp_servers: list[Json] = []
    for server in _enabled_mcp_servers():
        server_name = str(server.get("name") or "").strip()
        if not server_name:
            continue
        server_entry: Json = {"name": server_name, "owner": "gateway_service", "source": "mcp_server"}
        if include_mcp_tools:
            tools: list[Json] = []
            try:
                for tool in _mcp_list_server_tools(server):
                    tool_name = str(tool.get("name") or "").strip()
                    if not tool_name:
                        continue
                    public_name = _mcp_public_name(server_name, tool_name)
                    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
                    entry = _tool_entry(
                        public_name,
                        str(tool.get("description") or f"MCP tool from {server_name}"),
                        schema,
                        owner="gateway_service",
                        source="mcp_tool",
                        risk="connector",
                    )
                    entry["server"] = server_name
                    entry["tool"] = tool_name
                    tools.append(entry)
                    if public_name not in seen_service:
                        service_side.append(entry)
                        seen_service.add(public_name)
                server_entry["tools"] = tools
                server_entry["tool_count"] = len(tools)
            except Exception as exc:
                server_entry["error"] = str(exc)[:300]
                server_entry["tool_count"] = 0
        mcp_servers.append(server_entry)

    workflows = _agent_planner_workflow_catalog()
    intents = _agent_planner_intent_catalog()
    return {
        "mode": "remote_agent_planner",
        "chat_only_upstream_role": "synthesis_only",
        "ownership_model": {
            "gateway_service": "service-owned pure/network/connectors may run before upstream synthesis",
            "downstream_client": "filesystem, shell, GUI, local agent, and caller-private tools run in the client workspace",
            "chat_only_upstream": "language synthesis only; no tool authority",
        },
        "workflows": workflows,
        "intents": intents,
        "service_side": service_side,
        "downstream_owned": downstream_owned,
        "http_actions": http_actions,
        "mcp_servers": mcp_servers,
        "counts": {
            "workflows": len(workflows),
            "intents": len(intents),
            "service_side": len(service_side),
            "downstream_owned": len(downstream_owned),
            "http_actions": len(http_actions),
            "mcp_servers": len(mcp_servers),
        },
    }


def _gateway_executes_user_side_tools_locally() -> bool:
    """Return True only for explicit local-proxy execution mode.

    The production default for Codex/Claude Code clients is that tools touching
    the user's filesystem, shell, GUI, or local agent runtime execute on the
    downstream client.  Gateway-side execution is kept only as an explicit
    local-proxy opt-in; delegation preferences must not grant the cloud service
    authority over the user's workspace.
    """
    gateway = _gateway_config()
    env_value = os.environ.get("GATEWAY_EXECUTE_USER_SIDE_TOOLS")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    if gateway.get("execute_user_side_tools_in_gateway") is True:
        return True
    return False


def _declared_tool_names_from_body(body: Json) -> set[str]:
    names: set[str] = set()
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        candidates: list[Any] = [tool.get("name")]
        function = tool.get("function")
        if isinstance(function, dict):
            candidates.append(function.get("name"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                names.add(candidate.strip())
                names.add(_normalize_tool_name(candidate.strip()))
    return {name for name in names if name}


def _tool_call_requires_downstream_execution(call: ToolCall, body: Json | None = None) -> bool:
    """Return True when a tool call must be surfaced to the downstream client.

    User-machine tools (filesystem, shell, GUI, local subagents) must not run in
    the Gateway service by default. Gateway-owned tools such as HTTP Actions,
    MCP server tools, network tools, pure utilities, and Gateway state tools can
    still execute in the service.
    """
    if _gateway_executes_user_side_tools_locally():
        return False
    normalized = _normalize_tool_call(call)
    tool = BUILTIN_TOOLS.get(normalized.name)
    canonical_name = tool.name if tool is not None else normalized.name

    if canonical_name == "multi_tool_use.parallel":
        tool_uses = normalized.arguments.get("tool_uses")
        if isinstance(tool_uses, list):
            for item in tool_uses:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("tool") or item.get("tool_name") or item.get("recipient_name")
                args = item.get("arguments") or item.get("input") or item.get("parameters") or {}
                if isinstance(name, str) and "." in name:
                    name = name.rsplit(".", 1)[-1]
                if isinstance(name, str) and _tool_call_requires_downstream_execution(
                    ToolCall(f"{normalized.call_id}_nested", name, args if isinstance(args, dict) else {}, item),
                    body,
                ):
                    return True

    if body is not None and _declared_tool_shadows_gateway_builtin(body, normalized.name):
        return True

    if tool is not None:
        if canonical_name == "JsonQuery":
            # JsonQuery(data=...) is a pure service helper.  JsonQuery(file_path=...)
            # reads the downstream workspace and must not run on the cloud Gateway
            # service unless explicit local-proxy execution is enabled.
            args = normalized.arguments if isinstance(normalized.arguments, dict) else {}
            reads_workspace_file = args.get("data") is None and bool(args.get("file_path") or args.get("path"))
            if reads_workspace_file:
                return True
        return tool.risk in USER_SIDE_TOOL_RISKS

    # Gateway-owned extension points.
    if _mcp_parse_public_name(normalized.name) or _http_action_by_name(normalized.name):
        return False

    # Caller-private/custom functions are owned by the downstream client when
    # the request declared their schema. Do not fake or fail them in Gateway.
    if body is not None:
        declared = _declared_tool_names_from_body(body)
        if normalized.name in declared or call.name in declared:
            return True
    return False


def _calls_require_downstream_execution(calls: list[ToolCall], body: Json | None = None) -> bool:
    return any(_tool_call_requires_downstream_execution(call, body) for call in calls)


def _select_local_planner_files(user_text: str, max_files: int) -> list[str]:
    roots = _extract_mentioned_paths(user_text)
    if not roots:
        roots = ["src", "README.md", "docs"]
    files: list[str] = []
    patterns_by_root: list[tuple[str, str]] = []
    for root in roots:
        normalized = root.rstrip("/") or "."
        try:
            resolved = _resolve_workspace_path(normalized)
        except Exception:
            continue
        if resolved.is_file():
            try:
                rel = str(resolved.relative_to(_workspace_root()))
                files.append(rel)
            except Exception:
                pass
        elif resolved.is_dir():
            if normalized.lower().endswith("docs"):
                patterns_by_root.append((normalized, "**/*.md"))
            elif normalized.lower().endswith("src") or "src" in normalized.lower():
                patterns_by_root.append((normalized, "**/*.py"))
            else:
                patterns_by_root.extend([(normalized, "**/*.py"), (normalized, "**/*.md")])
    for root, pattern in patterns_by_root:
        result = _execute_tool_call(ToolCall(f"planner_glob_{uuid.uuid4().hex}", "Glob", {"path": root, "pattern": pattern, "limit": max_files}, {}))
        if result.success:
            for line in result.content.splitlines():
                item = line.rstrip("/")
                if item and item not in files:
                    files.append(item)
                if len(files) >= max_files:
                    break
        if len(files) >= max_files:
            break
    return files[:max_files]


def _build_local_planner_context(user_text: str) -> str:
    gateway = _gateway_config()
    max_files = max(1, min(int(gateway.get("local_planner_max_files") or 24), 80))
    max_bytes = max(1000, min(int(gateway.get("local_planner_max_bytes_per_file") or 24000), 200000))
    sections: list[str] = []
    tree = _execute_tool_call(ToolCall(f"planner_tree_{uuid.uuid4().hex}", "Tree", {"path": ".", "max_depth": 3, "max_entries": 300}, {}))
    if tree.success:
        sections.append("## 本地工具结果：项目结构 Tree\n" + tree.content)
    files = _select_local_planner_files(user_text, max_files)
    if files:
        sections.append("## 本地工具结果：命中文件列表\n" + "\n".join(files))
    symbol_sections: list[str] = []
    for file_path in [f for f in files if f.endswith(".py")][:max_files]:
        symbols = _execute_tool_call(ToolCall(f"planner_symbols_{uuid.uuid4().hex}", "PythonSymbols", {"file_path": file_path}, {}))
        if symbols.success:
            symbol_sections.append(f"### {file_path}\n{symbols.content[:12000]}")
    if symbol_sections:
        sections.append("## 本地工具结果：Python 符号/类/函数\n" + "\n\n".join(symbol_sections))
    if files:
        read_many = _execute_tool_call(
            ToolCall(
                f"planner_read_{uuid.uuid4().hex}",
                "ReadManyFiles",
                {"paths": files, "max_files": max_files, "max_bytes_per_file": max_bytes},
                {},
            )
        )
        if read_many.success:
            sections.append("## 本地工具结果：关键文件内容\n" + read_many.content)
    return "\n\n".join(sections)


def _apply_local_planner_context(path: str, body: Json) -> Json:
    if not _gateway_executes_user_side_tools_locally():
        return body
    if not _should_build_local_planner_context(path, body):
        return body
    user_text = _last_user_text(path, body)
    lowered = user_text.lower()
    direct_read = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开")) and bool(_extract_mentioned_paths(user_text))
    if isinstance(body.get("gateway_context"), dict) and body["gateway_context"].get("compacted") and not direct_read:
        return body
    context = _build_local_planner_context(user_text)
    if not context.strip():
        return body
    if direct_read:
        prompt = (
            "Gateway 已经在本地真实读取用户点名的文件/路径。"
            "下面的工具结果是事实证据，不是提示词伪造的 tool call。"
            "请直接基于这些证据回答用户原始请求；如果用户要求只输出某个值，就只输出该值，不要再说需要读取文件。\n\n"
            "# 用户原始请求\n"
            f"{user_text}\n\n"
            "# Gateway 本地真实工具证据\n"
            f"{context}"
        )
        # Direct file-read prompts from Claude Code often arrive with a huge
        # harness (system reminders, skill lists, transcript summaries).  If we
        # keep that harness, weak upstreams may ignore the injected evidence and
        # answer "I will read the file" instead of using the already-read local
        # result.  For this branch the gateway has already executed the local
        # read, so preserve only generation knobs plus a minimal evidenced user
        # request.
        preserve_keys = {"model", "max_tokens", "max_output_tokens", "temperature", "top_p", "stream"}
        updated = {key: copy.deepcopy(value) for key, value in body.items() if key in preserve_keys}
        if "/responses" in path:
            updated["input"] = prompt
        elif "/messages" in path:
            updated["messages"] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        else:
            updated["messages"] = [{"role": "user", "content": prompt}]
    else:
        prompt = (
            "Gateway 已经在本地真实执行文件/符号/目录工具完成预分析。"
            "下面的工具结果是事实证据，不是提示词伪造的 tool call。"
            "请基于这些证据完成用户请求；如果证据不足，说明还需要哪些文件/工具。\n\n"
            "# 用户原始请求\n"
            f"{user_text}\n\n"
            "# Gateway 本地真实工具证据\n"
            f"{context}\n\n"
            "# 输出要求\n"
            "按 语义分析 / 逐个类或文件分析 / 调用与证据检查 / 反思调整 / 最终结论 输出。"
        )
        updated = _replace_last_user_text(path, body, prompt)
    updated.setdefault("gateway_context", {})
    updated["gateway_context"].update({"local_planner": True, "planner_evidence_chars": len(context)})
    return updated




_WORKSPACE_MUTATING_TOOL_RISKS = frozenset({"write_local", "execute_code"})


def _tool_may_mutate_workspace(tool: Any) -> bool:
    return bool(tool and str(getattr(tool, "risk", "")) in _WORKSPACE_MUTATING_TOOL_RISKS)


def _invalidate_tool_cache_scope(
    tool_cache: Any,
    workspace_cache_key: str,
    runtime_cache_key: str,
) -> None:
    if not tool_cache or not workspace_cache_key or not runtime_cache_key:
        return
    try:
        tool_cache.invalidate_scope(workspace_cache_key, runtime_cache_key)
    except Exception as exc:
        _logger.warning("Failed to invalidate tool cache scope after mutation: %s", exc)


def _record_failed_tool_result(
    call: ToolCall,
    result: ToolResult,
    *,
    started_at: float,
    retry_count: int,
    provider: str,
) -> ToolResult:
    failure_type = str(result.failure_type or "execution_failed")
    _record_tool_failure(
        tool_name=call.name,
        call_id=call.call_id,
        failure_type=failure_type,
        arguments_keys=sorted(call.arguments.keys()),
        content=result.content if result.content else "",
        execution_ms=time.time() - started_at,
        retry_count=max(0, int(retry_count)),
        provider=provider,
    )
    _record_tool_stat(call.name, False, failure_type)
    return result


def _execute_tool_call_impl(call: ToolCall, provider: str | None = None, client_id: str | None = None) -> ToolResult:
    import time as _time
    _start = _time.time()
    original_name = call.name
    call = _normalize_tool_call(call)

    # Permission check: verify tool execution is allowed for this client
    try:
        from .gateway_permissions import check_tool_permission
        allowed, reason = check_tool_permission(call.name, client_id, log=True)
        if not allowed:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"Permission denied: {reason}",
                success=False,
                failure_type="permission_denied",
            )
    except ImportError:
        _logger.debug("Permission module unavailable, allowing execution")
    except Exception as exc:
        _logger.warning(f"Permission check failed for {call.name}: {exc}")
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=f"Permission check error: {exc}",
            success=False,
            failure_type="permission_denied",
        )

    tool = BUILTIN_TOOLS.get(call.name)
    mcp_target = None if tool else _mcp_parse_public_name(call.name)
    http_action = None if tool or mcp_target else (_http_action_by_name(call.name) or _http_action_by_name(original_name))
    cfg = _gateway_config() if callable(_gateway_config) else _gateway_config
    raw_max_retries = cfg.get("tool_max_retries", 1) if isinstance(cfg, dict) else 1
    try:
        max_retries = max(0, int(raw_max_retries))
    except (TypeError, ValueError):
        max_retries = 1
    if http_action:
        try:
            max_retries = int(http_action.get("max_retries", 0) or 0)
        except (TypeError, ValueError):
            max_retries = 0
        max_retries = max(0, max_retries)
    provider = provider or "unknown"

    # Check tool result cache for cacheable read-only tools.  Cache keys must
    # include both the resolved client workspace and request runtime scope: the
    # same arguments such as {"file_path": "README.md"} or {"url": ...}
    # are different operations for different remote tenants/sessions/workspaces.
    _tool_cache = None
    _tool_cache_arguments = call.arguments
    workspace_cache_key = ""
    runtime_cache_key = ""
    tool_cache_allowed = True
    try:
        from .gateway_cache import get_tool_result_cache
        _tool_cache = get_tool_result_cache()
        if tool:
            try:
                workspace_cache_key = str(_workspace_root())
            except Exception:
                workspace_cache_key = "workspace:unavailable"
            try:
                from . import gateway_builtin_tools as _bt
                runtime_cache_key = _bt._runtime_scope_key()
                tool_cache_allowed = not _bt._scope_has_active_exec_sessions()
            except Exception:
                runtime_cache_key = "runtime:unavailable"
        if tool and tool_cache_allowed and _tool_cache.is_cacheable(call.name):
            _tool_cache_arguments = dict(call.arguments)
            _tool_cache_arguments["__gateway_workspace_cache_key"] = workspace_cache_key
            _tool_cache_arguments["__gateway_runtime_cache_key"] = runtime_cache_key
            cached = _tool_cache.get(call.name, _tool_cache_arguments)
            if cached is not None:
                return ToolResult(call_id=call.call_id, name=call.name, content=cached, success=True)
    except Exception:
        _tool_cache = None

    last_exc: Exception | None = None
    last_result: ToolResult | None = None
    mutates_workspace = _tool_may_mutate_workspace(tool)
    for attempt in range(max_retries + 1):
        try:
            if mcp_target:
                server_name, mcp_tool_name = mcp_target
                server = _mcp_server_by_name(server_name)
                if not server:
                    result = ToolResult(
                        call_id=call.call_id, name=call.name,
                        content=f"connector_required: MCP server {server_name} is not configured or enabled",
                        success=False, failure_type="connector_required",
                    )
                    return _record_failed_tool_result(
                        call,
                        result,
                        started_at=_start,
                        retry_count=attempt,
                        provider=provider,
                    )
                content = _mcp_call_tool(server, mcp_tool_name, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            if http_action:
                content = _call_http_action(http_action, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            if not tool:
                result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"ToolNotFound: {call.name} is not implemented or installed in Gateway runtime",
                    success=False, failure_type="tool_not_found",
                )
                return _record_failed_tool_result(
                    call,
                    result,
                    started_at=_start,
                    retry_count=attempt,
                    provider=provider,
                )
            content = tool.handler(call.arguments)
            if mutates_workspace:
                _invalidate_tool_cache_scope(_tool_cache, workspace_cache_key, runtime_cache_key)
            _record_tool_stat(call.name, True)
            # Store in tool result cache for cacheable tools
            try:
                if _tool_cache and tool_cache_allowed and _tool_cache.is_cacheable(call.name):
                    _tool_cache.put(call.name, _tool_cache_arguments, content)
            except Exception:
                pass
            return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
        except (ToolExecutionError, subprocess.TimeoutExpired) as exc:
            last_exc = exc
            if mutates_workspace:
                _invalidate_tool_cache_scope(_tool_cache, workspace_cache_key, runtime_cache_key)
            if isinstance(exc, subprocess.TimeoutExpired):
                timeout_parts = [f"timeout: tool execution exceeded {exc.timeout}s"]
                if exc.output:
                    timeout_parts.append(f"stdout:\n{exc.output}")
                if exc.stderr:
                    timeout_parts.append(f"stderr:\n{exc.stderr}")
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content="\n".join(timeout_parts),
                    success=False, failure_type="timeout",
                )
            elif isinstance(exc, ToolExecutionError):
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"{exc.failure_type}: {exc}",
                    success=False, failure_type=exc.failure_type,
                )
            retryable = bool(isinstance(exc, ToolExecutionError) and getattr(exc, "retryable", False))
            if not retryable or attempt >= max_retries:
                return _record_failed_tool_result(
                    call,
                    last_result,
                    started_at=_start,
                    retry_count=attempt,
                    provider=provider,
                )
        except Exception as exc:
            # Non-transient error — do not retry
            _logger.warning("Tool %s failed with non-transient error: %s", call.name, exc)
            if mutates_workspace:
                _invalidate_tool_cache_scope(_tool_cache, workspace_cache_key, runtime_cache_key)
            result = ToolResult(
                call_id=call.call_id, name=call.name,
                content=f"execution_failed: {exc}",
                success=False, failure_type="execution_failed",
            )
            return _record_failed_tool_result(
                call,
                result,
                started_at=_start,
                retry_count=attempt,
                provider=provider,
            )
    # All attempts exhausted
    if last_result is None:
        last_result = ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=f"execution_failed: {last_exc or 'tool execution failed'}",
            success=False,
            failure_type="execution_failed",
        )
    return _record_failed_tool_result(
        call,
        last_result,
        started_at=_start,
        retry_count=max_retries,
        provider=provider,
    )


def _execute_tool_call(call: ToolCall, provider: str | None = None, client_id: str | None = None) -> ToolResult:
    """Execute one tool call and emit bounded-cardinality duration telemetry."""
    from .gateway_observability import observe_tool
    started = time.monotonic()
    tool = BUILTIN_TOOLS.get(call.name)
    if tool is not None:
        tool_class = "builtin"
        metric_name = tool.name
    elif _mcp_parse_public_name(call.name):
        tool_class = "mcp"
        metric_name = "mcp"
    elif _http_action_by_name(call.name):
        tool_class = "http_action"
        metric_name = "http_action"
    else:
        tool_class = "unknown"
        metric_name = "unknown"
    try:
        result = _execute_tool_call_impl(call, provider=provider, client_id=client_id)
    except Exception as exc:
        observe_tool(
            metric_name,
            tool_class=tool_class,
            success=False,
            failure_type=exc.__class__.__name__,
            duration_seconds=time.monotonic() - started,
        )
        raise
    observe_tool(
        metric_name,
        tool_class=tool_class,
        success=bool(result.success),
        failure_type=result.failure_type or "none",
        duration_seconds=time.monotonic() - started,
    )
    return result


def _direct_tool_result_payload(result: ToolResult) -> Json:
    protocol_content = _encode_tool_result_content(result.content, not result.success)
    payload: Json = {
        "id": result.call_id,
        "object": "gateway.tool_result",
        "name": result.name,
        "success": result.success,
        "failure_type": result.failure_type,
        "content": result.content,
        "fake_prompt_tools": False,
        "openai_chat": {
            "role": "tool",
            "tool_call_id": result.call_id,
            "content": protocol_content,
        },
        "openai_responses": {
            "type": "function_call_output",
            "call_id": result.call_id,
            "output": protocol_content,
        },
        "anthropic": {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": result.content,
            "is_error": not result.success,
        },
    }
    return payload


def execute_direct_tool_call(body: Json, *, path: str = "/tools/call", client_id: str | None = None) -> Json:
    scope_body = _request_scope_body(body, client_id)
    with _workspace_scope(_request_workspace_root(scope_body), scope_body):
        try:
            calls = _direct_tool_calls_from_body(body)
        except ToolExecutionError as exc:
            _record_agent_runtime_request_event(
                path,
                scope_body,
                event_type="direct_tool_error",
                workflow="direct_tool",
                step="invalid_input",
                summary=str(exc)[:500],
                metadata={
                    "owner": "gateway_service",
                    "source": "direct_tool_endpoint",
                    "success": False,
                    "failure_type": exc.failure_type,
                },
            )
            raise BadRequestError(str(exc), detail={"failure_type": exc.failure_type}) from exc
        if _calls_require_downstream_execution(calls, body):
            tool_names = [call.name for call in calls]
            failure_type = "direct_user_side_tool_requires_downstream_client"
            _record_agent_runtime_request_event(
                path,
                scope_body,
                event_type="direct_tool_error",
                workflow="direct_tool",
                step="downstream_required",
                summary=", ".join(tool_names)[:500],
                metadata={
                    "owner": "downstream_client",
                    "source": "direct_tool_endpoint",
                    "success": False,
                    "failure_type": failure_type,
                    "tool_names": tool_names,
                },
            )
            raise BadRequestError(
                "direct user-side tool execution is disabled in Gateway cloud mode; "
                "run this tool in the downstream client workspace",
                detail={"failure_type": failure_type, "tool_names": tool_names},
            )
        _record_agent_runtime_request_event(
            path,
            scope_body,
            event_type="direct_tool_execute",
            workflow="direct_tool",
            step="execute",
            summary=", ".join(call.name for call in calls)[:500],
            metadata={
                "owner": "gateway_service",
                "source": "direct_tool_endpoint",
                "tool_count": len(calls),
                "tool_names": [call.name for call in calls],
            },
        )
        results = [_execute_tool_call(call, provider="direct", client_id=client_id) for call in calls]
        _record_agent_runtime_request_event(
            path,
            scope_body,
            event_type="direct_tool_result",
            workflow="direct_tool",
            step="result",
            summary=", ".join(f"{result.name}:{'ok' if result.success else result.failure_type or 'failed'}" for result in results)[:500],
            metadata={
                "owner": "gateway_service",
                "source": "direct_tool_endpoint",
                "tool_count": len(results),
                "success": all(result.success for result in results),
                "tool_names": [result.name for result in results],
            },
        )
    payloads = [_direct_tool_result_payload(result) for result in results]
    if len(payloads) == 1:
        return payloads[0]
    return {
        "object": "gateway.tool_results",
        "success": all(result.success for result in results),
        "results": payloads,
        "fake_prompt_tools": False,
    }



def _looks_like_context_rejection(text: str) -> bool:
    lowered = (text or "").lower()
    needles = (
        "text you sent is too long",
        "too long",
        "context length",
        "maximum context",
        "input is too large",
        "send it in parts",
        "simplify the content",
        "文本太长",
        "内容过长",
        "上下文",
        "分段发送",
    )
    return any(needle in lowered for needle in needles)

def token_count_response(body: Json, *, path: str = "/v1/messages/count_tokens", client_id: str | None = None) -> Json:
    scope_body = _request_scope_body(body, client_id)
    with _workspace_scope(_request_workspace_root(scope_body), scope_body):
        _record_agent_runtime_request_event(
            path,
            scope_body,
            event_type="token_count_execute",
            workflow="token_count",
            step="estimate",
            summary="estimate request input tokens",
            metadata={
                "owner": "gateway_service",
                "source": "token_count_endpoint",
            },
        )
        response = {"input_tokens": _body_token_estimate(body)}
        _record_agent_runtime_request_event(
            path,
            scope_body,
            event_type="token_count_result",
            workflow="token_count",
            step="result",
            summary=f"input_tokens={response['input_tokens']}",
            metadata={
                "owner": "gateway_service",
                "source": "token_count_endpoint",
                "success": True,
                "input_tokens": response["input_tokens"],
            },
        )
        return response


def record_gateway_public_endpoint(
    path: str,
    body: Json,
    *,
    resource: str,
    action: str,
    response: Json | None = None,
    success: bool = True,
    failure_type: str | None = None,
    client_id: str | None = None,
) -> None:
    """Record a Gateway-owned public API boundary in runtime audit scope."""
    scope_body = _request_scope_body(body, client_id)
    with _workspace_scope(_request_workspace_root(scope_body), scope_body):
        response = response if isinstance(response, dict) else {}
        metadata: Json = {
            "owner": "gateway_service",
            "source": f"{resource}_endpoint",
            "resource": resource,
            "action": action,
            "success": bool(success),
        }
        if failure_type:
            metadata["failure_type"] = failure_type
        object_value = response.get("object")
        if object_value is not None:
            metadata["object"] = object_value
        response_id = response.get("id")
        if response_id is not None:
            metadata["id"] = response_id
        if resource == "models" and isinstance(response.get("data"), list):
            metadata["model_count"] = len(response.get("data") or [])
        _record_agent_runtime_request_event(
            path,
            scope_body,
            event_type=f"{resource}_{'result' if success else 'error'}",
            workflow=f"gateway_{resource}",
            step=action,
            summary=f"{resource}:{action}:{'ok' if success else failure_type or 'failed'}",
            metadata=metadata,
        )



def _build_tool_round_response(path: str, calls: list[ToolCall], results: list[ToolResult], fallback_response: Json) -> Json:
    model = fallback_response.get("model") or _config_env("UPSTREAM_MODEL", "")
    usage = fallback_response.get("usage") or {"input_tokens": 0, "output_tokens": 0}
    strategy = "gateway_local_planner_tool_round" if results else "gateway_downstream_tool_request"
    if "/messages" in path:
        content: list[dict] = []
        for call in calls:
            content.append({"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments})
        text = _response_text(path, fallback_response)
        if text:
            content.append({"type": "text", "text": text})
        # Match native tool path: assistant contains tool_use blocks only,
        # stop_reason "tool_use" signals the client to send tool_result back.
        # tool_result blocks belong in the user message, not the assistant message.
        has_tool_use = any(b.get("type") == "tool_use" for b in content)
        return {
            "id": fallback_response.get("id") or f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "tool_use" if has_tool_use else "end_turn",
            "stop_sequence": None,
            "usage": usage,
            "gateway_context": {"strategy": strategy},
        }
    if "/responses" in path:
        output_items: list[dict] = []
        for call in calls:
            output_items.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            })
        for result in results:
            output_items.append({"type": "function_call_output", "call_id": result.call_id, "output": result.content})
        text = _response_text(path, fallback_response)
        if text:
            output_items.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
        return {
            "id": fallback_response.get("id") or f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "model": model,
            "output": output_items,
            "status": "completed",
            "usage": usage,
            "gateway_context": {"strategy": strategy},
        }
    tool_calls = []
    for call in calls:
        tool_calls.append({
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
        })
    choice = {"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": tool_calls}, "finish_reason": "tool_calls"}
    return {
        "id": fallback_response.get("id") or f"chatcmpl_gateway_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [choice],
        "usage": usage,
        "gateway_context": {"strategy": strategy},
    }


def _collect_synthetic_upstream_calls(path: str, response: Json) -> tuple[list[ToolCall], list[ToolResult]]:
    calls = _extract_tool_calls(path, response) or _extract_text_tool_calls(path, response)
    return calls, []


def _has_tool_result_in_messages(path: str, body: Json) -> bool:
    """Return True if the request already contains tool_result blocks,
    meaning the client (e.g. Claude Code) already processed tool_use and
    sent back results.  In that case the gateway must NOT re-surface
    planner tool rounds to avoid an infinite loop."""
    messages = body.get("messages") or body.get("input") or []
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") in ("tool_result", "function_call_output", "custom_tool_call_output"):
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_result", "function_call_output"):
                    return True
        # Also check for tool role messages (OpenAI chat format)
        if msg.get("role") == "tool":
            return True
    return False


def _collect_local_planner_tool_rounds(path: str, body: Json) -> tuple[list[ToolCall], list[ToolResult]]:
    ctx = body.get("gateway_context") if isinstance(body.get("gateway_context"), dict) else {}
    should_surface = bool(ctx.get("local_planner"))
    if not should_surface and not _gateway_executes_user_side_tools_locally():
        should_surface = _should_build_local_planner_context(path, body)
    if not should_surface:
        return [], []
    # If the client already sent back tool_result blocks, the tools were
    # already surfaced in a previous turn — do not surface again.
    if _has_tool_result_in_messages(path, body):
        return [], []
    user_text = _last_user_text(path, body)
    if not user_text:
        return [], []
    calls: list[ToolCall] = []
    results: list[ToolResult] = []

    def add_user_side_call(name: str, arguments: dict) -> None:
        candidate_map = {
            "Read": ("Read", "read", "open", "view_file"),
            "LS": ("LS", "ls", "list", "list_files", "list_directory"),
            "Glob": ("Glob", "glob", "file_search", "find_files"),
            "Tree": ("Tree", "tree"),
            "Bash": ("Bash", "bash", "shell", "exec_command"),
        }
        call = _declared_or_fallback_tool_call(
            body,
            f"client_required_{uuid.uuid4().hex}",
            candidate_map.get(name, (name, name.lower())),
            name,
            dict(arguments),
            {"gateway_downstream_tool_request": True},
        )
        if call is None:
            if name == "Read":
                target = str(arguments.get("path") or arguments.get("file_path") or ".")
                call = _shell_tool_call_for_downstream(body, _read_shell_command(target))
            elif name in {"LS", "Glob", "Tree"}:
                target = str(arguments.get("path") or ".")
                call = _shell_tool_call_for_downstream(
                    body,
                    _project_inspection_shell_command(target),
                    {"gateway_downstream_tool_request": True, "fallback_shell_tool": True},
                )
        if call is not None:
            calls.append(call)

    if not _gateway_executes_user_side_tools_locally():
        paths = _extract_mentioned_paths(user_text)
        lowered = user_text.lower()
        read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开"))
        analyze_intent = any(token in lowered for token in ("分析", "analyze", "review", "审查", "梳理", "check", "inspect"))
        if not paths:
            if any(token in lowered for token in ("current directory", "当前目录", "list files", "列出文件", "目录")):
                add_user_side_call("LS", {"path": "."})
            elif _text_requests_project_inspection(user_text):
                add_user_side_call("LS", {"path": "."})
                add_user_side_call("Glob", {"path": ".", "pattern": "**/*.py"})
                add_user_side_call("Glob", {"path": ".", "pattern": "**/*.md"})
            return calls, results
        for raw_path in paths[: max(1, min(int(_gateway_config().get("local_planner_max_files") or 24), 12))]:
            cleaned = raw_path.rstrip("/") or "."
            looks_file = bool(re.search(r"\.[A-Za-z0-9]{1,12}$", pathlib.PurePosixPath(cleaned).name))
            if read_intent or looks_file:
                add_user_side_call("Read", {"path": cleaned})
            elif analyze_intent:
                add_user_side_call("Tree", {"path": cleaned, "max_depth": 3, "max_entries": 300})
            else:
                add_user_side_call("LS", {"path": cleaned})
        return calls, results

    def run(name: str, arguments: dict) -> None:
        call = ToolCall(f"planner_surfaced_{uuid.uuid4().hex}", name, arguments, {"gateway_local_planner_surface": True})
        result = _execute_tool_call(call, provider="local_planner_surface")
        if result.success:
            calls.append(call)
            results.append(result)
    run("Tree", {"path": ".", "max_depth": 3, "max_entries": 300})
    files = _select_local_planner_files(user_text, max(1, min(int(_gateway_config().get("local_planner_max_files") or 24), 12)))
    if files:
        run("ReadManyFiles", {"paths": files, "max_files": len(files), "max_bytes_per_file": max(2000, min(int(_gateway_config().get("local_planner_max_bytes_per_file") or 24000), 48000))})
    return calls, results


def _tool_schema_name_local(tool: Json) -> str:
    if not isinstance(tool, dict):
        return ""
    func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    return str(func.get("name") or tool.get("name") or "").strip()


def _tool_schema_required_local(tool: Json) -> list[str]:
    if not isinstance(tool, dict):
        return []
    func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    params = func.get("parameters") or func.get("input_schema") or tool.get("parameters") or tool.get("input_schema") or {}
    if not isinstance(params, dict):
        return []
    return [str(item) for item in (params.get("required") or []) if isinstance(item, str)]


def _forced_request_tool_name(body: Json) -> str:
    forced = _forced_tool_name_from_choice(body.get("tool_choice"))
    if forced:
        return forced
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, str) and tool_choice in {"required", "any"}:
        tools = [tool for tool in (body.get("tools") or []) if isinstance(tool, dict)]
        names = [_tool_schema_name_local(tool) for tool in tools]
        names = [name for name in names if name]
        if len(names) == 1:
            return names[0]
    return ""


def _infer_forced_tool_arguments(path: str, name: str, body: Json) -> Json:
    user_text = _last_user_text(path, body)
    normalized = _normalize_tool_name(name)
    if normalized in {"calculator", "calc", "gateway__calculator"}:
        expr = ""
        code_match = re.search(r"`([^`]+)`", user_text)
        if code_match:
            expr = code_match.group(1).strip()
        if not expr:
            matches = re.findall(r"[-+*/%(). 0-9]+", user_text)
            matches = [m.strip() for m in matches if re.search(r"\d", m) and re.search(r"[+*/%-]", m)]
            if matches:
                expr = max(matches, key=len).strip()
        return {"expression": expr or user_text.strip()}
    if normalized in {"get_current_time", "current_time"}:
        tz_match = re.search(r"\b[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?\b", user_text)
        if tz_match:
            return {"timezone": tz_match.group(0)}
        if any(token in user_text for token in ("上海", "中国", "北京时间", "Asia/Shanghai")):
            return {"timezone": "Asia/Shanghai"}
        return {}
    if normalized in {"Read", "FileInfo", "LS", "Tree", "Glob", "Grep"}:
        paths = _extract_mentioned_paths(user_text)
        if normalized == "Glob":
            return {"pattern": paths[-1] if paths else "*"}
        if normalized == "Grep":
            quoted = re.findall(r"`([^`]+)`|['\"]([^'\"]+)['\"]", user_text)
            pattern = next((a or b for a, b in quoted if (a or b)), "")
            return {"pattern": pattern or user_text.strip(), "path": paths[-1] if paths else "."}
        return {"path": paths[-1] if paths else "."}
    if normalized in {"Bash", "exec_command", "shell", "shell_command"}:
        return {"command": _extract_explicit_shell_command_request(user_text) or user_text.strip()}
    if normalized == "echo_probe":
        match = re.search(r"(?:value|echo|probe)\s+[`'\"]?([A-Za-z0-9_.:-]+)", user_text, flags=re.I)
        return {"value": match.group(1) if match else user_text.strip()}

    tool = next((tool for tool in (body.get("tools") or []) if _tool_schema_name_local(tool) == name), None)
    required = _tool_schema_required_local(tool or {})
    if len(required) == 1:
        return {required[0]: user_text.strip()}
    return {}


def _synthetic_tool_response(path: str, call: ToolCall, model: str = "") -> Json:
    if "/messages" in path:
        return {
            "id": f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    if "/responses" in path:
        return {
            "id": f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "model": model,
            "output": [{
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            }],
            "status": "completed",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
    return {
        "id": f"chatcmpl_gateway_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _forced_gateway_tool_round(path: str, body: Json) -> tuple[ToolCall | None, ToolResult | None, Json | None]:
    if _has_tool_result_in_messages(path, body):
        return None, None, None
    name = _forced_request_tool_name(body)
    if not name:
        return None, None, None
    call = _normalize_tool_call(ToolCall(
        call_id=f"gateway_forced_{uuid.uuid4().hex}",
        name=name,
        arguments=_infer_forced_tool_arguments(path, name, body),
        raw={"gateway_forced_tool_choice": True},
    ))
    if _tool_call_requires_downstream_execution(call, body):
        return call, None, _synthetic_tool_response(path, call, str(body.get("model") or _config_env("UPSTREAM_MODEL", "")))
    if call.name in BUILTIN_TOOLS or _mcp_parse_public_name(call.name) or _http_action_by_name(call.name):
        return call, _execute_tool_call(call, provider="gateway_forced_tool_choice"), None
    # Gateway cannot execute caller-private custom functions.  For forced
    # tool_choice, surface the required protocol-level call to the downstream
    # client instead of pretending the upstream supports native tools.
    return call, None, _synthetic_tool_response(path, call, str(body.get("model") or _config_env("UPSTREAM_MODEL", "")))


def run_tool_orchestration(path: str, body: Json, client: NativeProxyClient | None = None, client_id: str | None = None) -> Json:
    _logger.debug("run_tool_orchestration called for path: %s", path)
    scope_body = _request_scope_body(body, client_id)
    workspace_root = _request_workspace_root(scope_body)
    _logger.debug("Workspace root resolved to: %s", workspace_root)

    # Check if tools are present in body
    tools_in_body = body.get("tools", [])
    _logger.debug("Tools in request body: %d tools", len(tools_in_body))
    if len(tools_in_body) > 0:
        _logger.debug("First 3 tools: %s", [t.get('name', t.get('function', {}).get('name', 'unknown')) for t in tools_in_body[:3]])

    with _workspace_scope(workspace_root, scope_body):
        return _run_tool_orchestration_scoped(path, body, client, client_id)


def _convert_response_to_path(target_path: str, response: Json) -> Json:
    """Convert a response to the format matching the target path.

    Detects the actual response format and converts if needed.
    """
    from .gateway_protocol import (
        _is_anthropic_response, _is_openai_chat_response, _is_openai_responses_response,
        _from_openai_chat_response, _from_anthropic_response_to_openai,
        _from_openai_chat_to_responses_response, _from_responses_response_to_openai,
    )
    # If already in the target format, return as-is
    if "/chat/completions" in target_path and _is_openai_chat_response(response):
        return response
    if "/responses" in target_path and _is_openai_responses_response(response):
        return response
    if "/messages" in target_path and _is_anthropic_response(response):
        return response
    # Convert to target format
    if "/chat/completions" in target_path:
        if _is_anthropic_response(response):
            return _from_anthropic_response_to_openai(response)
        if _is_openai_responses_response(response):
            return _from_responses_response_to_openai(response)
    if "/responses" in target_path:
        if _is_openai_chat_response(response):
            return _from_openai_chat_to_responses_response(response)
        if _is_anthropic_response(response):
            return _from_openai_chat_to_responses_response(_from_anthropic_response_to_openai(response))
    if "/messages" in target_path:
        if _is_openai_chat_response(response):
            return _from_openai_chat_response(target_path, response)
        if _is_openai_responses_response(response):
            return _from_openai_chat_response(target_path, _from_responses_response_to_openai(response))
    return response


def _attach_request_gateway_context(response: Json, request_body: Json) -> Json:
    """Carry planner/runtime metadata from the synthesized request to response.

    The outer planner can preexecute service-side tools before the chat-only
    upstream is called.  Without this propagation the final user-facing
    response looks like a plain upstream answer, hiding which planner workflow
    ran and making the Agent runtime hard to observe/debug.
    """
    source_ctx = request_body.get("gateway_context")
    if not isinstance(source_ctx, dict) or not source_ctx:
        return response
    interesting = {
        key: copy.deepcopy(value)
        for key, value in source_ctx.items()
        if key in {
            "agent_planner",
            "local_planner",
            "planner_evidence_chars",
            "compacted",
            "strategy",
            "chat_only_synthesis",
            "upstream_tools_stripped",
            "agent_planner_strict_every_turn",
            "planner_has_evidence",
        }
    }
    if not interesting:
        return response
    updated = dict(response)
    target_ctx = updated.get("gateway_context") if isinstance(updated.get("gateway_context"), dict) else {}
    merged = dict(target_ctx)
    merged.update(interesting)
    updated["gateway_context"] = merged
    return updated


def _run_tool_orchestration_scoped(path: str, body: Json, client: NativeProxyClient | None = None, client_id: str | None = None) -> Json:
    gateway_cfg = _gateway_config()
    mode = str(os.environ.get("GATEWAY_TOOL_MODE") or gateway_cfg.get("tool_mode") or "orchestrate").lower()
    memory_body = _inject_recalled_memories(path, body)
    # Gateway-owned tools may execute in the service. User-machine tools
    # (Read/LS/Bash/Skill/GUI/local agents) are surfaced to the downstream
    # client by default so they run against the user's real workspace/machine.
    direct_response = None
    if _weak_upstream_text_tools_active(mode):
        direct_response = _direct_local_file_read_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_local_skill_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_local_bash_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_downstream_tool_request_response(path, memory_body)
    if direct_response is not None:
        _remember_conversation_turn(path, body, direct_response)
        return direct_response
    upstream = client or NativeProxyClient()
    from .gateway_config import _upstream_protocol
    upstream_protocol = _upstream_protocol()

    # Convert request to upstream protocol format
    upstream_path, converted_body = _convert_request_to_upstream(path, memory_body, upstream_protocol)

    # Override model with configured upstream model
    upstream_model = _config_env("UPSTREAM_MODEL", "") or _upstream_config().get("model", "")
    if upstream_model and "model" in converted_body:
        converted_body["model"] = upstream_model

    if mode in {"passthrough", "native_passthrough", "proxy"}:
        response = upstream.forward(upstream_path, converted_body)
        # Convert response back to downstream format
        response = _convert_response_to_downstream(path, response, upstream_protocol)
        _verify_native_if_forced(path, memory_body, response)
        _remember_conversation_turn(path, body, response)
        return response
    max_rounds = _configured_max_tool_rounds(gateway_cfg)
    full_cfg = load_config()
    context_cfg = _context_config()
    fanout_response = _run_context_fanout(path, memory_body, upstream, full_cfg)
    if fanout_response is not None:
        _remember_conversation_turn(path, body, fanout_response)
        return fanout_response
    forced_call, forced_result, forced_response = (None, None, None)
    if _weak_upstream_text_tools_active(mode):
        forced_call, forced_result, forced_response = _forced_gateway_tool_round(path, memory_body)
    if forced_response is not None:
        _remember_conversation_turn(path, body, forced_response)
        return forced_response
    if _weak_upstream_text_tools_active(mode):
        memory_body = _preexecute_gateway_owned_planner_tool(path, memory_body, client_id=client_id)
    # Agent Planner evidence must survive upstream context limits.  First record
    # the full, un-compacted tool evidence into planner state; then compact the
    # request for the weak upstream; finally inject the compact planner summary
    # back into the post-compaction body.
    _agent_prepare_upstream_body(path, memory_body)
    compacted_body = _maybe_compact_request_for_upstream(path, memory_body, context_cfg)
    request_body = _agent_prepare_upstream_body(path, _apply_local_planner_context(path, compacted_body))
    if _weak_upstream_text_tools_active(mode) and _should_use_chat_only_synthesis_boundary(request_body):
        pre_synthesis_body = request_body
        request_body = _chat_only_synthesis_body(request_body)
        _record_chat_only_synthesis_boundary_event(
            path,
            pre_synthesis_body,
            request_body,
            source="non_streaming",
            scope_body=body,
        )
    else:
        request_body = _merge_builtin_tools(path, request_body)

    # --- Intelligence Enhancement ---
    # Analyze the user question and enhance the system prompt with insights.
    # This runs before upstream conversion so the enhanced prompt flows through normally.
    try:
        from .gateway_intelligence import enhance_intelligence, _intelligence_config, get_intelligence_summary
        intel_cfg = _intelligence_config(full_cfg.get("intelligence") if isinstance(full_cfg.get("intelligence"), dict) else None)
        if intel_cfg.enabled:
            intel_result = enhance_intelligence(request_body.get("messages", []), intel_cfg)
            # Build system prompt enhancement
            prompt_parts = []
            if intel_result.system_prompt:
                prompt_parts.append(intel_result.system_prompt)
            if intel_result.should_reflect and intel_result.reflection_prompt:
                prompt_parts.append(intel_result.reflection_prompt)
            if prompt_parts:
                enhancement = "\n\n".join(prompt_parts)
                msgs = request_body.get("messages", [])
                if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
                    existing = str(msgs[0].get("content") or "")
                    if enhancement not in existing:
                        msgs[0]["content"] = existing + "\n\n" + enhancement
                    request_body["messages"] = msgs
            _logger.debug("Intelligence: %s", get_intelligence_summary(intel_result))
    except Exception as exc:
        _logger.debug("Intelligence enhancement skipped: %s", exc)

    # Keep Gateway planner/runtime metadata for response attachment and local
    # synthesis guards, but do not forward that internal envelope upstream.
    response_context_body = request_body

    # Convert merged request to upstream format
    upstream_path, request_body = _convert_request_to_upstream(path, request_body, upstream_protocol)
    # Override model with configured upstream model
    if upstream_model and "model" in request_body:
        request_body["model"] = upstream_model
    tools_stripped = False
    original_tools = list(request_body.get("tools") or [])
    for _round in range(max_rounds):
        try:
            response = upstream.forward(upstream_path, request_body)
        except UpstreamHTTPError as exc:
            # Tool rejection fallback: if upstream rejects tools (400), strip and retry as text
            if exc.upstream_status == 400 and not tools_stripped and request_body.get("tools"):
                from .gateway_protocol import _inject_tools_as_text_prompt
                request_body = _without_tools(request_body)
                request_body = _inject_tools_as_text_prompt(request_body, original_tools)
                tools_stripped = True
                continue
            raise
        # Convert response to the format matching upstream_path for tool result appending
        upstream_response = _convert_response_to_path(upstream_path, response)
        # Convert response back to downstream format for tool extraction
        downstream_response = _convert_response_to_downstream(path, response, upstream_protocol)
        response_text = _response_text(path, downstream_response)
        if _looks_like_context_rejection(response_text):
            forced_fanout = _run_context_fanout(path, memory_body, upstream, full_cfg, force=True)
            if forced_fanout is not None:
                _remember_conversation_turn(path, body, forced_fanout)
                return forced_fanout
        if _chat_only_synthesis_active(response_context_body):
            _record_ignored_upstream_tool_attempt(
                path,
                response_context_body,
                downstream_response,
                source="non_streaming",
                scope_body=body,
            )
            downstream_response = _agent_apply_synthesis_refusal_fallback(path, response_context_body, downstream_response)
            downstream_response = _attach_request_gateway_context(downstream_response, response_context_body)
            _remember_conversation_turn(path, body, downstream_response)
            return downstream_response
        if forced_call is None:
            _verify_native_if_forced(path, request_body, downstream_response)
        calls = _extract_tool_calls(path, downstream_response)
        text_fallback = False
        if not calls:
            calls = _extract_text_tool_calls(path, downstream_response)
            text_fallback = bool(calls)
            if text_fallback:
                calls = _adapt_text_calls_for_declared_downstream_tools(memory_body, calls)
        # Intent detection fallback for weak models that can't generate tool calls
        if not calls:
            calls = _detect_intent_tool_calls(path, downstream_response, body)
            text_fallback = bool(calls)
        if not calls:
            if forced_call is not None and forced_result is not None:
                calls = [forced_call]
                results = [forced_result]
                if text_fallback:
                    request_body = _append_text_tool_results(upstream_path, request_body, upstream_response, calls, results)
                else:
                    request_body = _append_tool_results(upstream_path, request_body, upstream_response, results)
                continue
            # If the first weak-upstream reply did not emit tool calls, synthesize
            # a protocol-level user-side tool request (default) or surface the
            # explicit legacy local-planner results (opt-in local execution).
            # Do not re-run the planner after we already executed adapter/native
            # tools and the upstream has produced a final text answer; doing so
            # turns a completed answer back into another tool round.
            if _round == 0 and _text_says_tool_work_is_needed(response_text):
                planner_source_body = request_body if _gateway_executes_user_side_tools_locally() else memory_body
                planner_calls, planner_results = _collect_local_planner_tool_rounds(path, planner_source_body)
                if planner_calls:
                    synthetic_calls, synthetic_results = _collect_synthetic_upstream_calls(path, downstream_response)
                    all_calls = planner_calls + synthetic_calls
                    all_results = planner_results + synthetic_results
                    response = _build_tool_round_response(path, all_calls, all_results, downstream_response)
                    _remember_conversation_turn(path, body, response)
                    return response
            downstream_response = _attach_request_gateway_context(downstream_response, response_context_body)
            _remember_conversation_turn(path, body, downstream_response)
            return downstream_response

        if _calls_require_downstream_execution(calls, memory_body):
            # The upstream asked for a tool that must run on the user's machine
            # (filesystem/shell/GUI/local agent) or for a caller-private custom
            # function.  Surface a protocol-level tool request to Claude Code /
            # Codex instead of executing inside the Gateway service.
            native_response = (
                _convert_text_calls_to_downstream_response(path, calls, downstream_response, upstream_protocol)
                if text_fallback
                else downstream_response
            )
            _remember_conversation_turn(path, body, native_response)
            return native_response

        # --- Key design decision: who executes tools? ---
        # When delegate_tools_to_downstream=true, all remaining text tool calls
        # are converted to native protocol format and returned to the downstream
        # client (Claude Code / Codex) for execution.
        # delegate_tools_to_downstream only controls whether otherwise
        # Gateway-executable calls are surfaced to the downstream client. It is
        # not authorization to execute user-machine tools in the cloud service:
        # those were already returned above unless explicit local-proxy mode is
        # enabled with execute_user_side_tools_in_gateway/GATEWAY_EXECUTE_USER_SIDE_TOOLS.
        cfg_delegate = _gateway_config().get("delegate_tools_to_downstream")
        if cfg_delegate is None:
            # Default: gateway-owned tools execute in Gateway; user-machine
            # tools were already surfaced above. Explicit config can still
            # request legacy "delegate every tool" behavior.
            delegate = False
        else:
            delegate = bool(cfg_delegate)
        if delegate and not text_fallback:
            _remember_conversation_turn(path, body, downstream_response)
            return downstream_response
        if text_fallback and delegate:
            native_response = _convert_text_calls_to_downstream_response(
                path, calls, downstream_response, upstream_protocol,
            )
            _remember_conversation_turn(path, body, native_response)
            return native_response

        # Execute Gateway-owned/service-safe tools locally. User-machine tools
        # reach this point only in explicit local-proxy compatibility mode.
        results = [_execute_tool_call(call, client_id=client_id) for call in calls]
        if text_fallback:
            request_body = _append_text_tool_results(upstream_path, request_body, upstream_response, calls, results)
        else:
            request_body = _append_tool_results(upstream_path, request_body, upstream_response, results)
    raise GatewayError("max tool rounds exceeded", detail={"max_tool_rounds": max_rounds})


def _stream_mode_passthrough() -> bool:
    mode = _config_env("GATEWAY_TOOL_MODE", "orchestrate").lower()
    return mode in {"passthrough", "native_passthrough", "proxy"}


def _send_sse_headers(handler: BaseHTTPRequestHandler, status: int = 200) -> None:
    handler.send_response(status)
    handler.send_header("content-type", "text/event-stream; charset=utf-8")
    handler.send_header("cache-control", "no-cache")
    handler.send_header("connection", "close")
    handler.send_header("x-accel-buffering", "no")
    handler.end_headers()
    handler.close_connection = True


def _write_sse(handler: BaseHTTPRequestHandler, payload: Any, *, event: str | None = None) -> None:
    if event:
        handler.wfile.write(f"event: {event}\n".encode("utf-8"))
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for line in data.splitlines() or [""]:
        handler.wfile.write(f"data: {line}\n".encode("utf-8"))
    handler.wfile.write(b"\n")
    handler.wfile.flush()


def _stream_tool_start(handler: BaseHTTPRequestHandler, call_id: str, name: str) -> None:
    """Send SSE event when a tool call starts execution."""
    _write_sse(handler, {
        "type": "tool_start",
        "call_id": call_id,
        "name": name,
    }, event="tool_start")


def _stream_tool_progress(handler: BaseHTTPRequestHandler, call_id: str, name: str, progress: str) -> None:
    """Send SSE event for tool execution progress (for long-running tools)."""
    _write_sse(handler, {
        "type": "tool_progress",
        "call_id": call_id,
        "name": name,
        "progress": progress,
    }, event="tool_progress")


def _stream_tool_end(handler: BaseHTTPRequestHandler, call_id: str, name: str, success: bool, content: str) -> None:
    """Send SSE event when a tool call completes."""
    _write_sse(handler, {
        "type": "tool_end",
        "call_id": call_id,
        "name": name,
        "success": success,
        "content": content,
    }, event="tool_end")


def _stream_tool_error(handler: BaseHTTPRequestHandler, call_id: str, name: str, error: str) -> None:
    """Send SSE event when a tool call fails."""
    _write_sse(handler, {
        "type": "tool_error",
        "call_id": call_id,
        "name": name,
        "error": error,
    }, event="tool_error")


# ---------------------------------------------------------------------------
# Native tool verification
# ---------------------------------------------------------------------------

def _verify_native_if_forced(path: str, body: Json, response: Json) -> None:
    """Verify that native tool calls are present when tool_choice forces them.

    Raises NativeToolVerificationError if the upstream fails to return native
    tool calls when the request requires them.
    """
    from .gateway_errors import NativeToolVerificationError

    tool_choice = body.get("tool_choice")
    if not tool_choice:
        return

    # Only check when tool_choice forces a specific function
    is_forced = False
    if isinstance(tool_choice, str):
        is_forced = tool_choice in {"required", "any"}
    elif isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type", "")
        is_forced = choice_type in {"function", "tool", "required"}

    if not is_forced:
        return

    # Check if response contains native tool calls
    calls = _extract_tool_calls(path, response)
    if not calls:
        # Also check for text-based tool calls as fallback
        text_calls = _extract_text_tool_calls(path, response)
        if not text_calls:
            raise NativeToolVerificationError(
                "upstream failed to return native tool calls when tool_choice forced a function call",
                detail={"path": path, "tool_choice": tool_choice},
            )


def _native_tool_signal(path: str, response: Json) -> bool:
    """Check if a response contains native tool call signals.

    Returns True if the response indicates native tool calls were made.
    """
    if path.startswith("/v1/chat/completions"):
        choices = response.get("choices") or []
        for choice in choices:
            message = choice.get("message") or {}
            if message.get("tool_calls"):
                return True
            if message.get("function_call"):
                return True
            if choice.get("finish_reason") in {"tool_calls", "function_call"}:
                return True
        return False
    elif path.startswith("/v1/responses"):
        output = response.get("output") or []
        for item in output:
            if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                return True
        return False
    elif path.startswith("/v1/messages"):
        content = response.get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        return False
    return False


def _is_forced_tool_choice(path: str, body: Json) -> bool:
    """Check if tool_choice forces a specific tool call.

    Returns True if the request requires a specific tool to be called.
    """
    tool_choice = body.get("tool_choice")
    if not tool_choice:
        return False

    if path.startswith("/v1/chat/completions"):
        if isinstance(tool_choice, dict):
            return tool_choice.get("type") == "function"
        return tool_choice in {"required", "any"}
    elif path.startswith("/v1/responses"):
        if isinstance(tool_choice, dict):
            return tool_choice.get("type") == "function"
        return tool_choice in {"required", "any"}
    elif path.startswith("/v1/messages"):
        if isinstance(tool_choice, dict):
            return tool_choice.get("type") == "tool"
        return False
    return False


def _probe_body(path: str, model: str | None = None) -> Json:
    """Create a minimal request body for probing native tool support.

    Uses the echo_probe tool to test if the upstream properly handles
    native tool calls.
    """
    if path.startswith("/v1/chat/completions") or path == "/v1/chat/completions":
        return {
            "model": model or "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Say hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "echo_probe",
                        "description": "Return the input value. Used to verify real native tool calling.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string", "description": "The value to echo back"},
                            },
                            "required": ["value"],
                        },
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "echo_probe"}},
            "max_tokens": 100,
        }
    elif path.startswith("/v1/responses"):
        return {
            "model": model or "gpt-4o-mini",
            "input": "Say hello",
            "tools": [
                {
                    "type": "function",
                    "name": "echo_probe",
                    "description": "Return the input value. Used to verify real native tool calling.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string", "description": "The value to echo back"},
                        },
                        "required": ["value"],
                    },
                }
            ],
            "tool_choice": {"type": "function", "name": "echo_probe"},
        }
    else:
        # Anthropic Messages format
        return {
            "model": model or "claude-3-haiku-20240307",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Say hello"}],
            "tools": [
                {
                    "name": "echo_probe",
                    "description": "Return the input value. Used to verify real native tool calling.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string", "description": "The value to echo back"},
                        },
                        "required": ["value"],
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "echo_probe"},
        }


def run_native_probe(path: str, client: NativeProxyClient | None = None) -> Json:
    """Run a probe to check if the upstream supports native tool calls.

    Returns a status object indicating whether native tools are supported.
    """
    try:
        body = _probe_body(path)
        upstream = client or NativeProxyClient()
        response = upstream.forward(path, body)
        calls = _extract_tool_calls(path, response)
        if calls:
            return {
                "status": "ok",
                "native_tools": True,
                "probe_tool": calls[0].name,
                "message": "Native tool calls working correctly",
            }
        text_calls = _extract_text_tool_calls(path, response)
        if text_calls:
            return {
                "status": "partial",
                "native_tools": False,
                "text_fallback": True,
                "message": "Upstream returned text-based tool calls (no native support)",
            }
        return {
            "status": "unsupported",
            "native_tools": False,
            "message": "Upstream did not return any tool calls",
        }
    except Exception as exc:
        return {
            "status": "error",
            "native_tools": False,
            "error": str(exc),
            "message": f"Probe failed: {exc}",
        }
