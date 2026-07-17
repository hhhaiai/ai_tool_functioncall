# =============================================================================
# Streaming tool event parsing and SSE orchestration
# Owns downstream SSE emission for passthrough and gateway-orchestrated tool calls.
# =============================================================================

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from .gateway_protocol import (
    _is_responses_tool_call_type,
    _legacy_function_call_id,
    _responses_tool_call_arguments_string,
    _responses_tool_call_name,
)

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .gateway_app import ToolResult


# =============================================================================
# SSE / stream parsing utilities
# =============================================================================

def _parse_sse_line(line: str) -> tuple[None, None] | tuple[str, str]:
    """Parse an SSE line (or multi-line block). Returns (event_name, data).
    'data: X' returns (None, X). 'event: foo' returns (foo, '').
    Multi-line 'event: foo\ndata: X' returns (foo, X)."""
    if "\n" in line:
        parts = line.split("\n")
        event_name, event_data = None, ""
        for p in parts:
            p = p.rstrip("\r\n")
            if not p or p.startswith(":"):
                continue
            if p.startswith("event:"):
                event_name = p[6:].strip()
            elif p.startswith("data:"):
                event_data = p[5:].lstrip()
            elif ": " in p:
                k, v = p.split(": ", 1)
                if k == "event":
                    event_name = v
            elif p.startswith("data:"):
                event_data = p[5:].lstrip()
                # Strip trailing ')' which appears in test data with malformed JSON (e.g. ...}]})")
                if event_data.endswith(")"):
                    event_data = event_data[:-1]
        return event_name, event_data
    line = line.rstrip("\r\n")
    if not line or line.startswith(":"):
        return None, None
    if line.startswith("data:"):
        data_val = line[5:].lstrip()
        if data_val.endswith(")"):
            data_val = data_val[:-1]
        return None, data_val
    if line.startswith("event:"):
        return line[6:].strip(), ""
    if ": " in line:
        key, val = line.split(": ", 1)
        return key, val
    return line, ""


def _recover_tool_calls_from_malformed(data: str) -> list[dict]:
    """Extract tool_calls from malformed JSON using a character-by-character parser
    that tracks string boundaries to find the actual JSON structure."""
    calls = []
    # Find all tool_calls objects by scanning for the "tool_calls" key
    tc_key_pattern = re.compile(r'"tool_calls"\s*:\s*\[')
    for tc_match in tc_key_pattern.finditer(data):
        start = tc_match.end()
        # Find the matching ']' of the tool_calls array
        bracket_depth = 0
        in_string = False
        i = start
        while i < len(data):
            c = data[i]
            if c == '"' and (i == 0 or data[i-1] != '\\'):
                in_string = not in_string
            elif not in_string:
                if c == '[':
                    bracket_depth += 1
                elif c == ']':
                    if bracket_depth == 0:
                        break
                    bracket_depth -= 1
            i += 1
        array_content = data[start:i]
        # Parse individual tool_call objects from the array
        brace_depth = 0
        in_str = False
        obj_start = None
        for j, c in enumerate(array_content):
            if c == '"' and (j == 0 or array_content[j-1] != '\\'):
                in_str = not in_str
            elif not in_str:
                if c == '{':
                    if obj_start is None:
                        obj_start = j
                    brace_depth += 1
                elif c == '}':
                    brace_depth -= 1
                    if brace_depth == 0 and obj_start is not None:
                        obj_text = array_content[obj_start:j+1]
                        call = _parse_tool_call_object(obj_text)
                        if call:
                            calls.append(call)
                        obj_start = None
        # Also handle trailing partial object (no closing brace) at array end
        if obj_start is not None and bracket_depth == 0:
            trailing = array_content[obj_start:].rstrip(']})\n\r\t ')
            if trailing.startswith('{'):
                call = _parse_tool_call_object(trailing + '}')
                if call:
                    calls.append(call)
    # Deduplicate by call_id, prefer entries with non-empty name
    seen: dict[str, dict] = {}
    for c in calls:
        cid = c.get("call_id") or ""
        if cid not in seen or (c.get("name") and not seen[cid].get("name")):
            seen[cid] = c
    return list(seen.values())


def _parse_tool_call_object(text: str) -> dict | None:
    """Parse a single tool_call JSON object, extracting index/id/name/arguments."""
    # Try valid JSON first
    try:
        obj = json.loads(text)
        idx = obj.get("index", 0)
        tc = obj if "type" not in obj else {}
        for t in obj.get("tool_calls", []):
            func = t.get("function", {})
            return {
                "call_id": t.get("id", ""),
                "name": func.get("name", ""),
                "arguments": func.get("arguments", ""),
            }
        # Direct format
        func = obj.get("function", {})
        return {
            "call_id": obj.get("id", ""),
            "name": func.get("name", ""),
            "arguments": func.get("arguments", ""),
        }
    except json.JSONDecodeError:
        pass
    # Regex fallback for malformed text
    result: dict = {}
    # call_id: "id":"VALUE" or "call_id":"VALUE"
    m = re.search(r'"(?:id|call_id)"\s*:\s*"([^"]*)"', text)
    if m:
        result["call_id"] = m.group(1)
    # name: "name":"VALUE"
    m = re.search(r'"name"\s*:\s*"([^"]*)"', text)
    if m:
        result["name"] = m.group(1)
    # arguments: "arguments":"VALUE" — handle escaped quotes
    m = re.search(r'"arguments"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        result["arguments"] = m.group(1)
    return result if result else None


def _detect_streaming_tool_calls_from_sse(
    path: str, event: str | None, data: str | None
) -> list[dict]:
    """Parse SSE data into tool call dicts. Handles OpenAI/Anthropic/Responses formats."""
    if data is None or data == "":
        return []
    if event == "[DONE]":
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        # Try to recover partial tool_calls from malformed JSON
        return _recover_tool_calls_from_malformed(data)
    calls = []
    # OpenAI /chat/completions
    if "/chat/completions" in path:
        choices = payload.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            tc_list = delta.get("tool_calls", [])
            for tc in tc_list:
                func = tc.get("function", {})
                calls.append({
                    "call_id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", ""),
                })
            legacy = delta.get("function_call")
            if isinstance(legacy, dict):
                name = legacy.get("name", "")
                calls.append({
                    "call_id": _legacy_function_call_id(name) if name else "",
                    "name": name,
                    "arguments": legacy.get("arguments", ""),
                })
    # Anthropic /messages
    elif "/messages" in path:
        if event == "content_block_start":
            cb = payload.get("content_block", {})
            if cb.get("type") == "tool_use":
                calls.append({
                    "call_id": cb.get("id", ""),
                    "name": cb.get("name", ""),
                    "arguments": json.dumps(cb.get("input", {})),
                })
        elif event == "content_block_delta":
            delta = payload.get("delta", {})
            if delta.get("type") == "input_json_delta":
                partial_json = delta.get("partial_json", "")
                if partial_json:
                    calls.append({
                        "call_id": "",
                        "name": "",
                        "arguments": partial_json,
                        "_partial": True,
                        "_index": payload.get("index", 0),
                    })
        elif event == "content_block_stop":
            calls.append({
                "call_id": "",
                "name": "",
                "arguments": "",
                "_block_stop": True,
                "_index": payload.get("index", 0),
            })
    # Responses /responses
    elif "/responses" in path:
        resp_type = payload.get("type", "")
        item = payload.get("item") or payload.get("output") or {}
        if resp_type in ("response.output_item.done", "response.function_call_arguments.done"):
            if isinstance(item, dict) and _is_responses_tool_call_type(item.get("type")):
                arguments = _responses_tool_call_arguments_string(item)
                calls.append({
                    "call_id": item.get("call_id") or item.get("id") or "",
                    "name": _responses_tool_call_name(item),
                    "arguments": arguments,
                })
        elif resp_type == "response.function_call_arguments.delta":
            delta_val = payload.get("delta", "")
            if isinstance(delta_val, str) and delta_val:
                calls.append({
                    "call_id": "",
                    "name": "",
                    "arguments": delta_val,
                    "_partial": True,
                    "_output_index": payload.get("output_index", 0),
                })
        elif resp_type == "response.output_item.added":
            if isinstance(item, dict) and _is_responses_tool_call_type(item.get("type")):
                arguments = _responses_tool_call_arguments_string(item)
                calls.append({
                    "call_id": item.get("call_id") or item.get("id") or "",
                    "name": _responses_tool_call_name(item),
                    "arguments": arguments,
                    "_initial": True,
                })
    # Unknown event name — try OpenAI format as fallback if path suggests it
    elif path and "/chat/completions" in path:
        choices = payload.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            tc_list = delta.get("tool_calls", [])
            for tc in tc_list:
                func = tc.get("function", {})
                calls.append({
                    "call_id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", ""),
                })
            legacy = delta.get("function_call")
            if isinstance(legacy, dict):
                name = legacy.get("name", "")
                calls.append({
                    "call_id": _legacy_function_call_id(name) if name else "",
                    "name": name,
                    "arguments": legacy.get("arguments", ""),
                })
    return calls


def _forced_tool_name(path: str, body: dict) -> str:
    """Extract forced tool name from request body, or '' if not forced."""
    tc = body.get("tool_choice")
    if tc is None:
        return ""
    if isinstance(tc, str):
        return ""  # "auto" or "none" — not forced
    if isinstance(tc, dict):
        # Responses: tool_choice.name
        if tc.get("name"):
            return tc["name"]
        # OpenAI: tool_choice.function.name
        fn = tc.get("function", {})
        if fn.get("name"):
            return fn["name"]
        # Anthropic: tool_choice.type=tool, tool_choice.name
        if tc.get("type") == "tool" and tc.get("name"):
            return tc["name"]
    return ""


# =============================================================================
# Streaming orchestration entry point
# =============================================================================


def run_streaming_orchestration(
    handler: Any, path: str, body: dict, client_id: str | None = None
) -> None:
    """Streaming orchestration with tool call support."""
    from .gateway_config import _configured_max_tool_rounds, _gateway_config, _upstream_protocol
    from .gateway_context import (
        _context_config,
        _inject_recalled_memories,
        _maybe_compact_request_for_upstream,
        _remember_conversation_turn,
    )
    from .gateway_proxy import NativeProxyClient
    from .gateway_protocol import _convert_request_to_upstream, _convert_response_to_downstream
    from .gateway_agent_planner import (
        apply_synthesis_refusal_fallback as _agent_apply_synthesis_refusal_fallback,
        prepare_upstream_body as _agent_prepare_upstream_body,
    )
    from .gateway_tool_runtime import (
        _append_text_tool_results,
        _append_tool_results,
        _adapt_text_calls_for_declared_downstream_tools,
        _attach_request_gateway_context,
        _calls_require_downstream_execution,
        _convert_text_calls_to_downstream_response,
        _convert_response_to_path,
        _detect_intent_tool_calls,
        _direct_local_bash_response,
        _direct_downstream_tool_request_response,
        _direct_local_file_read_response,
        _execute_tool_call,
        _extract_text_tool_calls,
        _extract_tool_calls,
        _request_scope_body,
        _request_workspace_root,
        _weak_upstream_text_tools_active,
        _workspace_scope,
    )

    _send_sse_headers(handler)

    try:
        gateway_cfg = _gateway_config()
        mode = str(os.environ.get("GATEWAY_TOOL_MODE") or gateway_cfg.get("tool_mode") or "orchestrate").lower()
        upstream_protocol = _upstream_protocol()

        if mode in {"passthrough", "native_passthrough", "proxy"}:
            _stream_upstream_passthrough(handler, path, body)
            return

        scope_body = _request_scope_body(body, client_id)
        with _workspace_scope(_request_workspace_root(scope_body), scope_body):
            _run_streaming_orchestration_scoped(
                handler,
                path,
                body,
                mode=mode,
                upstream_protocol=upstream_protocol,
                gateway_cfg=gateway_cfg,
                max_rounds=_configured_max_tool_rounds(gateway_cfg),
                upstream=NativeProxyClient(),
                context_cfg=_context_config(),
                client_id=client_id,
            )
    except Exception as exc:
        try:
            _write_sse(handler, {"error": str(exc)}, event="error")
            _write_sse(handler, "[DONE]", event="done")
        except Exception:
            pass


def _run_streaming_orchestration_scoped(
    handler: Any,
    path: str,
    body: dict,
    *,
    mode: str,
    upstream_protocol: str,
    gateway_cfg: dict,
    max_rounds: int,
    upstream: Any,
    context_cfg: dict,
    client_id: str | None = None,
) -> None:
    """Run streaming orchestration after workspace root has been resolved."""
    from .gateway_context import (
        _inject_recalled_memories,
        _maybe_compact_request_for_upstream,
        _remember_conversation_turn,
    )
    from .gateway_agent_planner import (
        apply_synthesis_refusal_fallback as _agent_apply_synthesis_refusal_fallback,
        prepare_upstream_body as _agent_prepare_upstream_body,
    )
    from .gateway_protocol import _convert_request_to_upstream, _convert_response_to_downstream
    from .gateway_tool_runtime import (
        _append_text_tool_results,
        _append_tool_results,
        _adapt_text_calls_for_declared_downstream_tools,
        _attach_request_gateway_context,
        _calls_require_downstream_execution,
        _chat_only_synthesis_active,
        _chat_only_synthesis_body,
        _convert_text_calls_to_downstream_response,
        _convert_response_to_path,
        _detect_intent_tool_calls,
        _direct_local_bash_response,
        _direct_downstream_tool_request_response,
        _direct_local_file_read_response,
        _direct_local_skill_response,
        _execute_tool_call,
        _extract_text_tool_calls,
        _extract_tool_calls,
        _preexecute_gateway_owned_planner_tool,
        _record_chat_only_synthesis_boundary_event,
        _record_ignored_upstream_tool_attempt,
        _should_use_chat_only_synthesis_boundary,
        _weak_upstream_text_tools_active,
    )

    memory_body = _inject_recalled_memories(path, body)
    if _weak_upstream_text_tools_active(mode):
        direct_response = _direct_local_file_read_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_local_skill_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_local_bash_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_downstream_tool_request_response(path, memory_body)
        if direct_response is not None:
            _stream_final_response(handler, path, direct_response)
            _remember_conversation_turn(path, body, direct_response)
            return
        memory_body = _preexecute_gateway_owned_planner_tool(path, memory_body, client_id=client_id)

    # Keep planner memory independent from weak-upstream context windows:
    # persist full evidence first, compact the transport payload second, then
    # re-inject the planner's compact summary after compaction.
    _agent_prepare_upstream_body(path, memory_body)
    compacted_body = _maybe_compact_request_for_upstream(path, memory_body, context_cfg)
    request_body = _agent_prepare_upstream_body(path, compacted_body)
    if _weak_upstream_text_tools_active(mode) and _should_use_chat_only_synthesis_boundary(request_body):
        pre_synthesis_body = request_body
        request_body = _chat_only_synthesis_body(request_body)
        _record_chat_only_synthesis_boundary_event(
            path,
            pre_synthesis_body,
            request_body,
            source="streaming",
            scope_body=body,
        )
    else:
        request_body = _merge_builtin_tools(path, request_body)

    # --- Intelligence Enhancement (streaming path) ---
    try:
        from .gateway_intelligence import enhance_intelligence, _intelligence_config
        from .gateway_config import load_config as _load_cfg
        _full = _load_cfg()
        _icfg = _intelligence_config(_full.get("intelligence") if isinstance(_full.get("intelligence"), dict) else None)
        if _icfg.enabled:
            _intel = enhance_intelligence(request_body.get("messages", []), _icfg)
            # Build system prompt enhancement (matches non-streaming path)
            _prompt_parts = []
            if _intel.system_prompt:
                _prompt_parts.append(_intel.system_prompt)
            if _intel.should_reflect and _intel.reflection_prompt:
                _prompt_parts.append(_intel.reflection_prompt)
            if _prompt_parts:
                _enhancement = "\n\n".join(_prompt_parts)
                _msgs = request_body.get("messages", [])
                if _msgs and isinstance(_msgs[0], dict) and _msgs[0].get("role") == "system":
                    _ex = str(_msgs[0].get("content") or "")
                    if _enhancement not in _ex:
                        _msgs[0]["content"] = _ex + "\n\n" + _enhancement
                    request_body["messages"] = _msgs
    except Exception as _exc:
        _logger.debug("Intelligence enhancement skipped: %s", _exc)

    # Keep the internal planner/runtime envelope for local synthesis guards and
    # response observability, but never forward that envelope to the upstream
    # model.  _convert_request_to_upstream strips gateway_context and other
    # Gateway-only routing fields from the actual upstream payload.
    response_context_body = request_body
    tool_authority_requested = (
        bool(memory_body.get("tools"))
        or memory_body.get("tool_choice") not in (None, "", "none")
    )
    upstream_path, request_body = _convert_request_to_upstream(path, request_body, upstream_protocol)
    # Keep a reusable non-streaming body for tool-result append helpers. The
    # production NativeProxyClient.stream() copies it and sets stream=true for
    # each upstream round; legacy/fake clients still use forward().
    request_body = dict(request_body)
    request_body["stream"] = False

    tools_stripped = False
    original_tools = list(request_body.get("tools") or [])
    for _round in range(max_rounds):
        use_upstream_stream = hasattr(upstream, "stream") and bool(getattr(upstream, "supports_streaming", True))
        round_allows_tools = (
            tool_authority_requested
            or bool(request_body.get("tools"))
            or request_body.get("tool_choice") not in (None, "", "none")
        )
        immediate_stream = (
            use_upstream_stream
            and not round_allows_tools
            and not _weak_upstream_text_tools_active(mode)
            and not _chat_only_synthesis_active(response_context_body)
        )
        emitter = _DownstreamDeltaEmitter(handler, path, source_path=upstream_path) if immediate_stream else None
        try:
            if use_upstream_stream:
                from .gateway_stream_state import UpstreamResponseAccumulator
                accumulator = UpstreamResponseAccumulator(upstream_path)
                stream_iterator = upstream.stream(upstream_path, request_body)
                try:
                    for stream_event in stream_iterator:
                        for delta in accumulator.feed(stream_event.event, stream_event.data):
                            if emitter is not None:
                                emitter.emit(delta, accumulator.metadata())
                finally:
                    close_stream = getattr(stream_iterator, "close", None)
                    if callable(close_stream):
                        close_stream()
                response = accumulator.finalize()
            else:
                response = upstream.forward(upstream_path, request_body)
        except Exception as exc:
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                raise
            from .gateway_errors import UpstreamHTTPError
            if isinstance(exc, UpstreamHTTPError) and exc.upstream_status == 400 and not tools_stripped and request_body.get("tools"):
                from .gateway_protocol import _inject_tools_as_text_prompt, _without_tools
                request_body = _without_tools(request_body)
                request_body = _inject_tools_as_text_prompt(request_body, original_tools)
                tools_stripped = True
                continue
            _write_sse(handler, {"error": str(exc)}, event="error")
            _write_sse(handler, "[DONE]", event="done")
            return

        upstream_response = _convert_response_to_path(upstream_path, response)
        downstream_response = _convert_response_to_downstream(path, response, upstream_protocol)
        if _chat_only_synthesis_active(response_context_body):
            _record_ignored_upstream_tool_attempt(
                path,
                response_context_body,
                downstream_response,
                source="streaming",
                scope_body=body,
            )
            downstream_response = _agent_apply_synthesis_refusal_fallback(path, response_context_body, downstream_response)
            downstream_response = _attach_request_gateway_context(downstream_response, response_context_body)
            _stream_final_response(handler, path, downstream_response)
            _remember_conversation_turn(path, body, downstream_response)
            return
        calls = _extract_tool_calls(path, downstream_response)
        if calls and not round_allows_tools:
            # Never grant tool authority merely because an upstream emitted an
            # unsolicited tool object. If text was already sent it cannot be
            # replaced safely, so terminate the stream explicitly.
            if emitter is not None and emitter.emitted:
                _write_sse(handler, {"error": "upstream emitted an unauthorized tool call after streaming text"}, event="error")
                _write_sse(handler, "[DONE]", event="done")
                return
            calls = []
        text_fallback = False
        if not calls and round_allows_tools:
            calls = _extract_text_tool_calls(path, downstream_response)
            text_fallback = bool(calls)
            if text_fallback:
                calls = _adapt_text_calls_for_declared_downstream_tools(memory_body, calls)
        if not calls and _round == 0 and round_allows_tools:
            calls = _detect_intent_tool_calls(path, downstream_response, body)
            text_fallback = bool(calls)

        if not calls:
            downstream_response = _attach_request_gateway_context(downstream_response, response_context_body)
            if emitter is None or not emitter.finish(downstream_response):
                _stream_final_response(handler, path, downstream_response)
            _remember_conversation_turn(path, body, downstream_response)
            return

        if _calls_require_downstream_execution(calls, memory_body):
            native_response = (
                _convert_text_calls_to_downstream_response(path, calls, downstream_response, upstream_protocol)
                if text_fallback
                else downstream_response
            )
            _stream_final_response(handler, path, native_response)
            _remember_conversation_turn(path, body, native_response)
            return

        results = []
        for call in calls:
            _write_sse(handler, {
                "type": "tool_start",
                "call_id": call.call_id,
                "name": call.name,
            }, event="tool_start")
            result = _execute_tool_call(call, provider="streaming", client_id=client_id)
            results.append(result)
            _write_sse(handler, {
                "type": "tool_result",
                "call_id": result.call_id,
                "name": result.name,
                "success": result.success,
                "failure_type": result.failure_type,
                "content": result.content,
            }, event="tool_result")

        if text_fallback:
            request_body = _append_text_tool_results(upstream_path, request_body, upstream_response, calls, results)
        else:
            request_body = _append_tool_results(upstream_path, request_body, upstream_response, results)

    _write_sse(handler, {"error": "max tool rounds exceeded"}, event="error")
    _write_sse(handler, "[DONE]", event="done")


def _tools_enabled_for_upstream() -> str:
    """Check if tools should be enabled for the upstream API."""
    from .gateway_config import _upstream_config
    cfg = _upstream_config()
    return str(cfg.get("tools_enabled", "adapter") or "adapter").strip().lower()


def _upstream_native_tools_capable() -> bool:
    """Return whether the active upstream profile is configured as native-tool capable."""
    from .gateway_config import _upstream_config
    cfg = _upstream_config()
    capabilities = cfg.get("capabilities") if isinstance(cfg.get("capabilities"), dict) else {}
    return bool(capabilities.get("supports_tools", False)) and bool(capabilities.get("supports_function_calls", False))


def _should_use_text_tool_adapter(tools_enabled: str, native_capable: bool) -> bool:
    """Decide whether gateway tools must be exposed as text-call instructions.

    ``auto`` is intentionally conservative: if the user marks the upstream as
    not supporting native tools/function-calls, do not send native ``tools``
    schemas upstream. Use the local gateway adapter instead so weak model APIs
    do not reject the request or hallucinate unsupported protocol objects.
    """
    if tools_enabled in {"text_only", "adapter", "prompt"}:
        return True
    if tools_enabled == "auto" and not native_capable:
        return True
    return False


def _maybe_compact_for_text_tool_adapter(path: str, body: dict) -> dict:
    """Compact bulky Claude Code/Codex harness payloads before text-tool injection.

    Weak upstreams that do not support native tools often have lower practical
    request-size limits than their advertised context window.  A Claude Code
    request can include a large system prompt, skill list, and tool schemas even
    for a one-line user prompt.  When we must downgrade native tools to the
    Gateway text adapter, compact that harness before adding our own adapter
    instructions so the upstream does not answer with a provider-level
    ``too long`` refusal.

    The limit is dynamic: max(8000, min(upstream.max_input_tokens * 0.45, config_cap)).
    Config cap defaults to 48000; set to 0 to disable.
    """
    try:
        from .gateway_config import _resolved_text_tool_adapter_compact_token_limit
        from .gateway_context import _body_token_estimate, _compact_request_for_upstream, _context_config
        limit = _resolved_text_tool_adapter_compact_token_limit()
        if limit <= 0 or _body_token_estimate(body) <= limit:
            return body
        context_cfg = dict(_context_config())
        context_cfg["enabled"] = True
        # Keep enough user intent while forcing large system-reminder blocks to
        # shrink.  ``summary_max_chars`` is per content block, so cap it below
        # the token budget rather than using the global context threshold.
        try:
            existing_summary = int(context_cfg.get("summary_max_chars") or 6000)
        except (TypeError, ValueError):
            existing_summary = 6000
        context_cfg["summary_max_chars"] = max(1000, min(existing_summary, 6000))
        return _compact_request_for_upstream(path, body, context_cfg, reason="weak_upstream_text_tools")
    except Exception:
        return body


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _client_can_handle_implicit_tools(body: dict) -> bool:
    gateway_context = body.get("gateway_context")
    if isinstance(gateway_context, dict):
        return _truthy(gateway_context.get("client_can_handle_implicit_tools"))
    return False


def _user_message_needs_tools(body: dict) -> bool:
    """Detect if user message indicates need for tools (file reading, code analysis, etc.)."""
    import re

    messages = body.get("messages", [])
    if not messages:
        return False

    # Get last user message
    last_user_msg = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                last_user_msg = content
            elif isinstance(content, list):
                # Extract text from content blocks
                last_user_msg = " ".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            break

    if not last_user_msg:
        return False

    text = last_user_msg.lower()
    tool_intent_patterns = [
        r'(分析|检查|查看|审查|梳理|整理)(这套|这个|当前|本地|本)?(项目|工程|代码|代码库|仓库|目录|文件夹|结构)',
        r'(读取|打开|列出|搜索|查找).*(代码|文件|目录|文件夹|仓库|代码库|函数|类)',
        r'(运行|执行|测试).*(命令|脚本|测试|代码)',
        r'\b(analyze|analyse|check|inspect|review|examine|investigate)\b.*\b(project|code|file|directory|repo|codebase)\b',
        r'\b(read|open|view|show|display|cat)\b.*\b(file|code)\b',
        r'\b(list|show|display)\b.*\b(files|directory|folder|contents)\b',
        r'\b(find|search|locate|grep)\b.*\b(file|code|function|class)\b',
        r'\b(run|execute|test)\b.*\b(command|script|test)\b',
        r'\b(what|how|where)\b.*\b(files|structure|organized|works)\b',
        r'^\s*(tree|ls|pwd|grep)\b',
        r'^\s*(cat|head|tail)\s+(\.|/|~|\S*[./]\S*)',
        r'^\s*find\s+(\.|/|~|\S*[./]\S*)',
    ]

    return any(re.search(pattern, text) for pattern in tool_intent_patterns)


def _merge_builtin_tools(path: str, body: dict) -> dict:
    """Merge builtin tools into request body. Respects tools_enabled config."""
    from .gateway_builtin_tools import BUILTIN_TOOLS
    from .gateway_mcp import _mcp_tool_schemas
    from .gateway_http_actions import _http_action_schemas
    from .gateway_protocol import _inject_tools_as_text_prompt
    from .gateway_mcp import _tool_schema_for_path

    import copy
    body = copy.deepcopy(body)

    tools_enabled = _tools_enabled_for_upstream()
    native_capable = _upstream_native_tools_capable()
    if tools_enabled == "native_only" and not native_capable:
        from .gateway_errors import GatewayError
        raise GatewayError("upstream profile is configured as native_only but capabilities disable native tools/function calls")
    if tools_enabled in {"off", "disabled", "false", "0", "none"}:
        body.pop("tools", None)
        body.pop("tool_choice", None)
        return body

    # Get existing tools
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        tools = []
    gateway_context = body.get("gateway_context") if isinstance(body.get("gateway_context"), dict) else {}
    caller_requested_tools = (
        bool(tools)
        or body.get("tool_choice") not in (None, "", "none")
        or bool(gateway_context.get("had_tools"))
    )

    if not caller_requested_tools:
        # Plain chat must stay plain for both native and adapter upstreams.
        # Implicit Gateway tools require an explicit client capability plus a
        # concrete tool intent; otherwise every request gains unnecessary tool
        # authority and true safe token streaming is impossible.
        if not (_client_can_handle_implicit_tools(body) and _user_message_needs_tools(body)):
            body.pop("tools", None)
            body.pop("tool_choice", None)
            return body
        _logger.debug("Tool-capable client message detected as needing implicit Gateway tools")

    # Add builtin tools
    for tool in BUILTIN_TOOLS.values():
        schema = _tool_schema_for_path(path, tool)
        tools.append(schema)

    # Add MCP tools
    mcp_schemas = _mcp_tool_schemas(path)
    tools.extend(mcp_schemas)

    # Add HTTP action tools
    action_schemas = _http_action_schemas(path)
    tools.extend(action_schemas)

    if _should_use_text_tool_adapter(tools_enabled, native_capable):
        # Strip native tools and inject as compact text prompt.  Compact first so
        # Claude Code/Codex harness metadata plus adapter instructions stay under
        # weak upstream request-size limits.
        from .gateway_protocol import _forced_tool_name_from_choice
        forced_tool_name = _forced_tool_name_from_choice(body.get("tool_choice"))
        body = _maybe_compact_for_text_tool_adapter(path, body)
        body.pop("tools", None)
        body.pop("tool_choice", None)
        body = _inject_tools_as_text_prompt(body, tools, forced_tool_name=forced_tool_name)
        return body

    body["tools"] = tools
    return body


def _stream_upstream_passthrough(handler: Any, path: str, body: dict) -> None:
    """Stream response directly from upstream through the bounded transport."""
    from .gateway_proxy import NativeProxyClient
    from .gateway_protocol import _convert_request_to_upstream

    client = NativeProxyClient()
    upstream_path, upstream_body = _convert_request_to_upstream(path, body, client.protocol)
    stream_iterator = client.stream(upstream_path, upstream_body)
    try:
        for stream_event in stream_iterator:
            if stream_event.data == "[DONE]":
                continue
            if "/messages" in path and client.protocol == "anthropic_messages":
                try:
                    payload = json.loads(stream_event.data)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict):
                    block = _format_sse_block(stream_event.event, payload)
                    for normalized in _normalize_anthropic_sse_block(block):
                        handler.wfile.write(normalized)
                        handler.wfile.flush()
                    continue
            _write_sse(handler, stream_event.data, event=stream_event.event)
    except (BrokenPipeError, ConnectionResetError):
        raise
    except Exception as exc:
        _write_sse(handler, {"error": str(exc)}, event="error")
    finally:
        close_stream = getattr(stream_iterator, "close", None)
        if callable(close_stream):
            close_stream()
        try:
            _write_sse(handler, "[DONE]", event="done")
        except (BrokenPipeError, ConnectionResetError):
            pass


def _format_sse_block(event: str | None, payload: dict) -> bytes:
    """Format one SSE event block with JSON data."""
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _normalize_anthropic_sse_block(block: bytes) -> list[bytes]:
    """Normalize Anthropic tool_use start blocks for Claude Code clients.

    Some OpenAI-compatible Anthropic adapters emit a complete ``tool_use.input``
    object inside ``content_block_start`` and never send a following
    ``input_json_delta``. Claude Code 2.1.x treats the start block as metadata
    and builds the executable tool input from ``input_json_delta`` events, so
    those streams arrive at the local tool runner as ``input: {}`` and fail with
    missing ``command``/``file_path``.

    Rewriting
    ``content_block_start(content_block.input={...})`` into a start block with
    empty input plus an immediate ``content_block_delta(input_json_delta)``
    preserves the Anthropic streaming contract shape Claude Code expects while
    keeping the original call id, name, and block index.
    """
    if not block:
        return []
    text = block.decode("utf-8", errors="replace")
    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return [block]
    data_text = "\n".join(data_lines)
    try:
        payload = json.loads(data_text)
    except json.JSONDecodeError:
        return [block]
    payload_type = payload.get("type") if isinstance(payload, dict) else None
    if event_name not in {None, "", "content_block_start"} and payload_type != "content_block_start":
        return [block]
    if not isinstance(payload, dict) or payload_type != "content_block_start":
        return [block]
    content_block = payload.get("content_block")
    if not isinstance(content_block, dict) or content_block.get("type") != "tool_use":
        return [block]
    tool_input = content_block.get("input")
    if not isinstance(tool_input, dict) or not tool_input:
        return [block]

    start_payload = dict(payload)
    start_content_block = dict(content_block)
    start_content_block["input"] = {}
    start_payload["content_block"] = start_content_block
    index = start_payload.get("index", 0)
    delta_payload = {
        "type": "content_block_delta",
        "index": index,
        "delta": {
            "type": "input_json_delta",
            "partial_json": json.dumps(tool_input, ensure_ascii=False),
        },
    }
    return [
        _format_sse_block("content_block_start", start_payload),
        _format_sse_block("content_block_delta", delta_payload),
    ]


def _iter_normalized_anthropic_sse_blocks(resp: Any):
    """Yield upstream SSE bytes, normalizing complete tool_use start blocks."""
    block = bytearray()
    for line in resp:
        block.extend(line)
        if line in {b"\n", b"\r\n"}:
            for normalized in _normalize_anthropic_sse_block(bytes(block)):
                yield normalized
            block.clear()
    if block:
        for normalized in _normalize_anthropic_sse_block(bytes(block)):
            yield normalized


def _stream_gateway_context(response: dict) -> dict | None:
    """Return stream-safe Gateway runtime metadata, if present.

    Non-streaming responses already expose ``gateway_context`` for observability.
    Streaming clients should get the same information without adding custom SSE
    event types that strict OpenAI/Anthropic clients may reject.  We therefore
    attach the metadata to existing terminal/final chunks where unknown fields
    are normally ignored by SDKs but remain visible to diagnostics.
    """
    ctx = response.get("gateway_context")
    if not isinstance(ctx, dict) or not ctx:
        return None
    return dict(ctx)


def _attach_stream_gateway_context(payload: dict, gateway_context: dict | None) -> dict:
    if not gateway_context:
        return payload
    updated = dict(payload)
    updated["gateway_context"] = gateway_context
    return updated


class _DownstreamDeltaEmitter:
    """Encode safe canonical text/reasoning deltas into downstream SSE."""

    def __init__(self, handler: Any, path: str, *, source_path: str | None = None) -> None:
        self.handler = handler
        self.path = path
        self.source_path = source_path or path
        self.emitted = False
        self.started = False
        self.response_id = ""
        self.model = ""
        self._chat_started: set[int] = set()
        self._chat_text: dict[int, list[str]] = {}
        self._chat_reasoning: dict[int, list[str]] = {}
        self._anthropic_block_kind: str | None = None
        self._anthropic_block_index = -1
        self._anthropic_source_index: int | None = None
        self._responses_item_ids: dict[int, str] = {}
        self._responses_parts: dict[tuple[int, int], list[str]] = {}

    def _update_metadata(self, metadata: dict) -> None:
        self.response_id = str(metadata.get("id") or self.response_id)
        self.model = str(metadata.get("model") or self.model)

    def emit(self, delta: Any, metadata: dict) -> None:
        text = str(getattr(delta, "text", "") or "")
        kind = str(getattr(delta, "kind", "text") or "text")
        if not text:
            return
        self._update_metadata(metadata)
        raw_index = int(getattr(delta, "index", 0) or 0)
        content_index = int(getattr(delta, "content_index", 0) or 0)
        if "/chat/completions" in self.path:
            choice_index = raw_index if "/chat/completions" in self.source_path else 0
            self._emit_chat(kind, text, choice_index)
        elif "/messages" in self.path:
            source_index = raw_index if "/messages" in self.source_path else 0
            self._emit_anthropic(kind, text, metadata, source_index)
        elif "/responses" in self.path:
            if kind == "text":
                output_index = raw_index if "/responses" in self.source_path else 0
                response_content_index = content_index if "/responses" in self.source_path else 0
                self._emit_responses_text(text, metadata, output_index, response_content_index)

    def _emit_chat(self, kind: str, text: str, choice_index: int) -> None:
        response_id = self.response_id or f"chatcmpl_gateway_{uuid.uuid4().hex}"
        self.response_id = response_id
        if choice_index not in self._chat_started:
            _write_sse(self.handler, {
                "id": response_id,
                "object": "chat.completion.chunk",
                "model": self.model,
                "choices": [{"index": choice_index, "delta": {"role": "assistant"}, "finish_reason": None}],
            })
            self._chat_started.add(choice_index)
            self.started = True
        target = self._chat_reasoning if kind == "reasoning" else self._chat_text
        target.setdefault(choice_index, []).append(text)
        field = "reasoning" if kind == "reasoning" else "content"
        _write_sse(self.handler, {
            "id": response_id,
            "object": "chat.completion.chunk",
            "model": self.model,
            "choices": [{"index": choice_index, "delta": {field: text}, "finish_reason": None}],
        })
        self.emitted = True

    def _close_anthropic_block(self) -> None:
        if self._anthropic_block_kind is None:
            return
        _write_sse(self.handler, {
            "type": "content_block_stop",
            "index": self._anthropic_block_index,
        }, event="content_block_stop")
        self._anthropic_block_kind = None
        self._anthropic_source_index = None

    def _emit_anthropic(self, kind: str, text: str, metadata: dict, source_index: int) -> None:
        if not self.started:
            usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
            _write_sse(self.handler, {
                "type": "message_start",
                "message": {
                    "id": self.response_id or f"msg_{uuid.uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "usage": {"input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": 0},
                },
            }, event="message_start")
            self.started = True
        block_kind = "thinking" if kind == "reasoning" else "text"
        if self._anthropic_block_kind != block_kind or self._anthropic_source_index != source_index:
            self._close_anthropic_block()
            self._anthropic_block_index += 1
            content_block = {"type": "thinking", "thinking": ""} if block_kind == "thinking" else {"type": "text", "text": ""}
            _write_sse(self.handler, {
                "type": "content_block_start",
                "index": self._anthropic_block_index,
                "content_block": content_block,
            }, event="content_block_start")
            self._anthropic_block_kind = block_kind
            self._anthropic_source_index = source_index
        delta_payload = (
            {"type": "thinking_delta", "thinking": text}
            if block_kind == "thinking"
            else {"type": "text_delta", "text": text}
        )
        _write_sse(self.handler, {
            "type": "content_block_delta",
            "index": self._anthropic_block_index,
            "delta": delta_payload,
        }, event="content_block_delta")
        self.emitted = True

    def _emit_responses_text(self, text: str, metadata: dict, output_index: int, content_index: int) -> None:
        if not self.started:
            response_id = self.response_id or f"resp_gateway_{uuid.uuid4().hex}"
            self.response_id = response_id
            response_base = {
                "id": response_id,
                "object": "response",
                "created_at": int(metadata.get("created_at") or time.time()),
                "status": "in_progress",
                "model": self.model,
                "output": [],
            }
            _write_sse(self.handler, {"type": "response.created", "response": response_base}, event="response.created")
            _write_sse(self.handler, {"type": "response.in_progress", "response": response_base}, event="response.in_progress")
            self.started = True
        if output_index not in self._responses_item_ids:
            self._responses_item_ids[output_index] = f"msg_{uuid.uuid4().hex}"
            item_id = self._responses_item_ids[output_index]
            _write_sse(self.handler, {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
            }, event="response.output_item.added")
        else:
            item_id = self._responses_item_ids[output_index]
        part_key = (output_index, content_index)
        if part_key not in self._responses_parts:
            self._responses_parts[part_key] = []
            _write_sse(self.handler, {
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }, event="response.content_part.added")
        self._responses_parts[part_key].append(text)
        _write_sse(self.handler, {
            "type": "response.output_text.delta",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "delta": text,
        }, event="response.output_text.delta")
        self.emitted = True

    def finish(self, response: dict) -> bool:
        if not self.emitted:
            return False
        gateway_context = _stream_gateway_context(response)
        if "/chat/completions" in self.path:
            final_choices = {
                int(choice.get("index") or 0): str(choice.get("finish_reason") or "stop")
                for choice in response.get("choices") or []
                if isinstance(choice, dict)
            }
            for position, choice_index in enumerate(sorted(self._chat_started)):
                terminal = {
                    "id": self.response_id or response.get("id", ""),
                    "object": "chat.completion.chunk",
                    "model": self.model or response.get("model", ""),
                    "choices": [{"index": choice_index, "delta": {}, "finish_reason": final_choices.get(choice_index, "stop")}],
                }
                _write_sse(
                    self.handler,
                    _attach_stream_gateway_context(terminal, gateway_context if position == len(self._chat_started) - 1 else None),
                )
        elif "/messages" in self.path:
            self._close_anthropic_block()
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            _write_sse(self.handler, _attach_stream_gateway_context({
                "type": "message_delta",
                "delta": {"stop_reason": response.get("stop_reason") or "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": int(usage.get("output_tokens") or 0)},
            }, gateway_context), event="message_delta")
            _write_sse(self.handler, {"type": "message_stop"}, event="message_stop")
        elif "/responses" in self.path:
            completed_output = []
            for output_index, item_id in sorted(self._responses_item_ids.items()):
                completed_content = []
                for (part_output_index, content_index), pieces in sorted(self._responses_parts.items()):
                    if part_output_index != output_index:
                        continue
                    text = "".join(pieces)
                    completed_part = {"type": "output_text", "text": text, "annotations": []}
                    _write_sse(self.handler, {
                        "type": "response.output_text.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "text": text,
                    }, event="response.output_text.done")
                    _write_sse(self.handler, {
                        "type": "response.content_part.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "part": completed_part,
                    }, event="response.content_part.done")
                    completed_content.append(completed_part)
                completed_item = {
                    "id": item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": completed_content,
                }
                _write_sse(self.handler, {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": completed_item,
                }, event="response.output_item.done")
                completed_output.append(completed_item)
            completed_response = dict(response)
            completed_response["output"] = completed_output
            if gateway_context:
                completed_response["gateway_context"] = gateway_context
            _write_sse(self.handler, {
                "type": "response.completed",
                "response": completed_response,
            }, event="response.completed")
        _write_sse(self.handler, "[DONE]", event="done")
        return True


def _stream_final_response(handler: Any, path: str, response: dict) -> None:
    """Stream the final response to client."""
    gateway_context = _stream_gateway_context(response)
    if "/chat/completions" in path:
        choices = response.get("choices", [])
        for choice in choices:
            message = choice.get("message", {})
            if message.get("tool_calls"):
                _write_sse(handler, {
                    "id": response.get("id", ""),
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": choice.get("index", 0),
                        "delta": {"tool_calls": message["tool_calls"]},
                        "finish_reason": None,
                    }],
                })
                _write_sse(handler, _attach_stream_gateway_context({
                    "id": response.get("id", ""),
                    "object": "chat.completion.chunk",
                    "choices": [{"index": choice.get("index", 0), "delta": {}, "finish_reason": "tool_calls"}],
                }, gateway_context))
                continue
            if message.get("content"):
                _write_sse(handler, {
                    "id": response.get("id", ""),
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": message["content"]},
                        "finish_reason": None,
                    }],
                })
        if not any(((choice.get("message") or {}).get("tool_calls")) for choice in choices):
            _write_sse(handler, _attach_stream_gateway_context({
                "id": response.get("id", ""),
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }, gateway_context))
    elif "/messages" in path:
        msg_id = response.get("id", f"msg_{uuid.uuid4().hex}")
        model = response.get("model", "")
        usage = dict(response.get("usage") or {})
        if "total_tokens" not in usage:
            input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            usage["input_tokens"] = input_tokens
            usage["output_tokens"] = output_tokens
            usage["total_tokens"] = input_tokens + output_tokens
        _write_sse(handler, {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0},
            },
        })
        content = response.get("content", [])
        for idx, item in enumerate(content):
            if not isinstance(item, dict):
                continue
            block_type = item.get("type")
            if block_type == "thinking":
                _write_sse(handler, {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "thinking", "thinking": ""},
                })
                _write_sse(handler, {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "thinking_delta", "thinking": item.get("thinking", "")},
                })
                _write_sse(handler, {
                    "type": "content_block_stop",
                    "index": idx,
                })
            elif block_type == "text":
                _write_sse(handler, {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                })
                _write_sse(handler, {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": item["text"]},
                })
                _write_sse(handler, {
                    "type": "content_block_stop",
                    "index": idx,
                })
            elif block_type == "tool_use":
                _write_sse(handler, {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "tool_use", "id": item.get("id", ""), "name": item.get("name", ""), "input": {}},
                })
                _write_sse(handler, {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(item.get("input", {}), ensure_ascii=False)},
                })
                _write_sse(handler, {
                    "type": "content_block_stop",
                    "index": idx,
                })
        stop_reason = response.get("stop_reason", "end_turn")
        _write_sse(handler, _attach_stream_gateway_context({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage.get("output_tokens", 0)},
        }, gateway_context))
        _write_sse(handler, {"type": "message_stop"})
    elif "/responses" in path:
        response_id = response.get("id") or f"resp_gateway_{uuid.uuid4().hex}"
        model = response.get("model", "")
        usage = dict(response.get("usage") or {})
        if "total_tokens" not in usage:
            input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            usage["input_tokens"] = input_tokens
            usage["output_tokens"] = output_tokens
            usage["total_tokens"] = input_tokens + output_tokens
        response_base = {
            "id": response_id,
            "object": "response",
            "created_at": response.get("created_at") or int(time.time()),
            "status": "in_progress",
            "model": model,
            "output": [],
            "usage": usage,
        }
        _write_sse(handler, {
            "type": "response.created",
            "response": response_base,
        }, event="response.created")
        _write_sse(handler, {
            "type": "response.in_progress",
            "response": response_base,
        }, event="response.in_progress")
        output = response.get("output", [])
        completed_output = []
        output_index = 0
        for item in output:
            if isinstance(item, dict) and item.get("type") == "message":
                item_id = str(item.get("id") or f"msg_{uuid.uuid4().hex}")
                role = str(item.get("role") or "assistant")
                started_item = {
                    "id": item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": role,
                    "content": [],
                }
                _write_sse(handler, {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": started_item,
                }, event="response.output_item.added")
                content_parts = item.get("content", [])
                completed_content = []
                for part_index, part in enumerate(content_parts):
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = str(part.get("text", ""))
                        started_part = {
                            "type": "output_text",
                            "text": "",
                            "annotations": part.get("annotations") or [],
                        }
                        _write_sse(handler, {
                            "type": "response.content_part.added",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": part_index,
                            "part": started_part,
                        }, event="response.content_part.added")
                        if text:
                            _write_sse(handler, {
                                "type": "response.output_text.delta",
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": part_index,
                                "delta": text,
                            }, event="response.output_text.delta")
                        completed_part = dict(started_part)
                        completed_part["text"] = text
                        _write_sse(handler, {
                            "type": "response.output_text.done",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": part_index,
                            "text": text,
                        }, event="response.output_text.done")
                        _write_sse(handler, {
                            "type": "response.content_part.done",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": part_index,
                            "part": completed_part,
                        }, event="response.content_part.done")
                        completed_content.append(completed_part)
                completed_item = {
                    "id": item_id,
                    "type": "message",
                    "status": "completed",
                    "role": role,
                    "content": completed_content,
                }
                _write_sse(handler, {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": completed_item,
                }, event="response.output_item.done")
                completed_output.append(completed_item)
                output_index += 1
            elif isinstance(item, dict) and item.get("type") == "function_call":
                item_id = str(item.get("id") or f"fc_{uuid.uuid4().hex}")
                call_item = {
                    "id": item_id,
                    "type": "function_call",
                    "call_id": item.get("call_id") or f"call_{uuid.uuid4().hex}",
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "",
                    "status": item.get("status") or "completed",
                }
                _write_sse(handler, {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": {**call_item, "arguments": ""},
                }, event="response.output_item.added")
                if call_item["arguments"]:
                    _write_sse(handler, {
                        "type": "response.function_call_arguments.done",
                        "output_index": output_index,
                        "item": call_item,
                    }, event="response.function_call_arguments.done")
                _write_sse(handler, {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": call_item,
                }, event="response.output_item.done")
                completed_output.append(call_item)
                output_index += 1
        completed_response = dict(response_base)
        completed_response.update({
            "status": response.get("status") or "completed",
            "output": completed_output,
        })
        if gateway_context:
            completed_response["gateway_context"] = gateway_context
        _write_sse(handler, {
            "type": "response.completed",
            "response": completed_response,
        }, event="response.completed")

    _write_sse(handler, "[DONE]", event="done")




def _streaming_tool_event_for_path(
    path: str,
    call_id: str,
    name: str,
    arguments: dict,
    result: "ToolResult",
    msg_id: str,
    index: int,
) -> list[tuple[str, dict]]:
    """Build streaming SSE events for a tool result. Returns list of (event_name, payload)."""
    events: list[tuple[str, dict]] = []
    args_str = json.dumps(arguments)
    if "/chat/completions" in path:
        events.append(("chatcmpl", {
            "id": msg_id,
            "object": "chat.completion.chunk",
            "choices": [{
                "index": index,
                "finish_reason": None,
                "delta": {"tool_calls": [{"id": call_id, "index": index, "function": {"name": name, "arguments": args_str}, "type": "function"}]},
            }],
        }))
        events.append(("chatcmpl", {
            "id": msg_id,
            "object": "chat.completion.chunk",
            "choices": [{"index": index, "finish_reason": "tool_calls", "delta": {}}],
        }))
    elif "/messages" in path:
        # Anthropic: content_block_start → content_block_delta → content_block_stop
        events.append(("content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        }))
        events.append(("content_block_delta", {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": args_str},
        }))
        events.append(("content_block_stop", {
            "type": "content_block_stop",
            "index": index,
        }))
    elif "/responses" in path:
        # Responses: output_item.added → function_call_arguments.done → output_item.done
        fc_id = f"fc_{uuid.uuid4().hex}"
        events.append(("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": "", "arguments": ""},
        }))
        events.append(("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": name, "arguments": args_str},
        }))
        events.append(("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": name, "arguments": args_str},
        }))
    return events


# =============================================================================
# Backward-compat re-exports (match original gateway_app.py public names)
# =============================================================================
# These symbols were originally defined in gateway_app.py.
# Re-export them from here so existing imports continue to work.

def _send_sse_headers(handler: Any) -> None:
    """Send SSE response headers."""
    from .gateway_http_security import send_cors_headers

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    send_cors_headers(handler)
    handler.end_headers()


def _write_sse(handler: Any, payload: Any, *, event: str | None = None) -> None:
    """Write an SSE event to the handler."""
    if event:
        handler.wfile.write(f"event: {event}\n".encode("utf-8"))
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for line in data.splitlines() or [""]:
        handler.wfile.write(f"data: {line}\n".encode("utf-8"))
    handler.wfile.write(b"\n")
    handler.wfile.flush()


__all__ = [
    "_parse_sse_line",
    "_recover_tool_calls_from_malformed",
    "_parse_tool_call_object",
    "_detect_streaming_tool_calls_from_sse",
    "_forced_tool_name",
    "run_streaming_orchestration",
    "_streaming_tool_event_for_path",
    # backward-compat stubs
    "_send_sse_headers",
    "_write_sse",
]
