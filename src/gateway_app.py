#!/usr/bin/env python3
"""Native tools/function-call gateway.

This server does NOT simulate tool calls with prompt JSON. It forwards native
`tools`, `tool_choice`, `tool_calls`, and Anthropic `tool_use` protocol objects
to an upstream provider that already supports them. If the upstream rejects or
fails a forced native tool call, the gateway fails fast instead of pretending.

This module is the main entry point that re-exports from submodules for backward
compatibility.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import types
from http.server import ThreadingHTTPServer

# Import all submodules for backward compatibility
from . import gateway_config as _gateway_config_module
from . import gateway_logging as _gateway_logging_module
from . import gateway_tool_runtime as _gateway_tool_runtime_module

from .gateway_errors import (
    GatewayError,
    UpstreamHTTPError,
    UpstreamTimeoutError,
    NativeToolVerificationError,
    DownstreamAuthError,
    GatewayBusyError,
    GatewayUnavailableError,
    ConfigError,
    ConfigConflictError,
    ToolExecutionError,
)
from .gateway_config import (
    CONFIG_PATH,
    load_config,
    load_config_with_revision,
    config_file_revision,
    save_config,
    _default_config,
    _deep_update,
    _upstream_profile_id,
    _normalize_upstream_profile,
    _sync_active_upstream,
    _profile_from_admin_form,
    _redacted_config,
    _config_env,
    _configured_max_tool_rounds,
    _normalize_request_path,
    _supported_public_paths,
    _upstream_config,
    _gateway_config,
    _configured_upstream_path,
    _configured_upstream_path_by_key,
    _upstream_protocol,
    _use_openai_chat_upstream,
    _force_upstream_stream_aggregate,
    _hash_secret,
    _secret_fingerprint,
    _hash_password,
    _verify_password,
    _password_hash_needs_upgrade,
    _downstream_key_id,
    _env_bool,
    _env_int,
    _env_float,
    _env_upstream_protocol,
)
from .gateway_logging import (
    REQUEST_LOG_PATH,
    STATS_PATH,
    SQLITE_LOG_PATH,
    SQLITE_READY,
    _sqlite_path,
    _sqlite_connect,
    _sqlite_init,
    _sqlite_import_legacy_logs_locked,
    _failure_log_path,
    _logging_backend,
    _sqlite_insert_tool_failure,
    _sqlite_record_tool_stat,
    _sqlite_record_request_stat,
    _sqlite_insert_request_log,
    _sqlite_stats_snapshot,
    _sqlite_tail_requests,
    _sqlite_tail_failures,
    _record_tool_failure as _record_tool_failure_low_level,
    _read_json_file,
    _write_json_file,
    _record_tool_stat,
    _record_request_stat,
    _redact_payload,
    _write_request_log,
    _tail_jsonl,
    _stats_snapshot,
    _tail_requests,
    _tail_failures,
    _tool_catalog_snapshot,
)
from .gateway_protocol import (
    _text_from_content,
    _openai_text_from_content,
    _anthropic_system_to_text,
    _anthropic_tools_to_openai,
    _openai_tools_to_anthropic,
    _anthropic_tool_choice_to_openai,
    _convert_anthropic_messages_to_openai,
    _preserve_anthropic_fields,
    _to_openai_chat_payload,
    _openai_tool_calls_from_response,
    _ensure_anthropic_message_response,
    _from_openai_chat_response,
    _last_user_text,
    _replace_last_user_text,
    _without_tools,
)
from .gateway_context import (
    _approx_token_count,
    _SUMMARY_CACHE,
    _context_config,
    _context_enabled,
    _body_token_estimate,
    _gateway_system_prompt,
    _content_contains_gateway_prompt,
    _inject_gateway_system_prompt,
    _memory_config,
    _memory_enabled,
    _json_object_from_maybe_string,
    _memory_session_key,
    _memory_workspace_key,
    _memory_extract_keywords,
    _memory_extract_request_text,
    _memory_summarize_turn,
    _sqlite_insert_memory,
    _remember_conversation_turn,
    _sqlite_recall_memories,
    _recall_conversation_memories,
    _memory_block,
    _allocate_context_budget,
    _detect_task_type,
    _inject_recalled_memories,
    _sqlite_tail_memories,
    _upstream_supports_native_tools,
    _summarize_via_llm,
    _compact_messages_with_summary,
    _trim_text_for_context,
    _trim_content_for_context,
    _compact_messages,
    _compact_request_for_upstream,
    _maybe_compact_request_for_upstream,
    _chunk_text_by_tokens,
    _fanout_source_text,
    _make_partial_prompt,
    _trim_partials_for_synthesis,
    _make_synthesis_prompt,
    _make_quality_review_prompt,
    _should_fanout_context,
    _run_context_fanout,
)
from .gateway_mcp import (
    McpSession,
    MCP_SESSIONS,
    MCP_SESSIONS_LOCK,
    MCP_TOOL_CATALOG_CACHE,
    MCP_SERVER_STATUS,
    MCP_PROTOCOL_VERSION,
    MCP_CATALOG_CACHE_TTL_SECONDS,
    _mcp_safe_component,
    _mcp_public_name,
    _mcp_legacy_public_name,
    _mcp_parse_public_name,
    _enabled_mcp_servers,
    _mcp_server_by_name,
    _mcp_env,
    _mcp_command,
    _mcp_write_message,
    _mcp_read_exact,
    _mcp_read_message,
    _mcp_request,
    _mcp_notify,
    _mcp_start,
    _mcp_initialize,
    _mcp_with_server,
    _mcp_session_key,
    _mcp_use_pool,
    _mcp_get_session,
    _mcp_close_sessions,
    _mcp_catalog_ttl,
    _mcp_cache_key,
    _mcp_set_status,
    _mcp_invalidate_server,
    _mcp_health_snapshot,
    _mcp_list_server_tools,
    _mcp_call_tool,
    _mcp_content_to_text,
    _mcp_tool_schemas,
)
from .gateway_http_actions import (
    _enabled_http_actions,
    _http_action_by_name,
    _http_action_schemas,
    _expand_action_value,
    _http_action_headers,
    _call_http_action,
)
from .gateway_proxy import NativeProxyClient
from .gateway_http_handler import (
    SUPPORTED_PATHS,
    MODEL_LIST_PATHS,
    TOKEN_COUNT_PATHS,
    DIRECT_TOOL_CALL_PATHS,
    GatewayHandler,
    _json_response,
    _safe_json_response,
    _text_response,
    _read_json,
    _semantic_cache_request_fingerprint,
    _parse_basic_auth,
    _check_admin,
    _check_downstream_key,
    _read_form,
    _error_payload,
    _redirect,
    _set_gateway_ready,
    _gateway_is_ready,
    _enforce_request_rate_limit,
    _capability_contract,
)
from .gateway_admin import (
    _client_snippet_context,
    _toml_string,
    _client_config_snippets,
    _render_client_config_ui,
    _render_admin_ui,
)

# Re-export tool runtime functions
from .gateway_tool_runtime import (
    _first_present,
    _clean_tool_string,
    _clean_text_tool_path,
    _normalize_relative_pattern,
    _copy_model_override,
    _has_requested_tools,
    _response_has_tool_calls,
    _extract_openai_tool_calls_for_stream,
    _fallback_response,
    _acquire_request_slot,
    _request_slot_scope,
    _get_marketplace,
    _normalize_tool_call,
    _direct_tool_call_from_body,
    _direct_tool_calls_from_body,
    _response_tool_call_from_item,
    _strip_xmlish_closing_tags,
    _parse_parameter_blocks,
    _inline_text_before_parameter_blocks,
    _repair_shell_command_spacing,
    _parse_text_tool_calls,
    _extract_tool_calls,
    _text_tool_call_fallback_enabled,
    _extract_text_tool_calls,
    _assistant_message_from_chat_response,
    _append_tool_results,
    _append_text_tool_results,
    _extract_mentioned_paths,
    _should_build_local_planner_context,
    _select_local_planner_files,
    _build_local_planner_context,
    _apply_local_planner_context,
    _execute_tool_call,
    _direct_tool_result_payload,
    execute_direct_tool_call,
    _looks_like_context_rejection,
    token_count_response,
    run_tool_orchestration,
    _run_tool_orchestration_scoped,
    _stream_mode_passthrough,
    _send_sse_headers,
    _write_sse,
    _stream_tool_start,
    _stream_tool_progress,
    _stream_tool_end,
    _stream_tool_error,
    _response_text,
    _verify_native_if_forced,
    _native_tool_signal,
    _is_forced_tool_choice,
    _probe_body,
    run_native_probe,
)

# Re-export streaming functions
from .gateway_streaming import (
    _parse_sse_line,
    _recover_tool_calls_from_malformed,
    _parse_tool_call_object,
    _detect_streaming_tool_calls_from_sse,
    _forced_tool_name,
    _merge_builtin_tools,
    run_streaming_orchestration,
    _streaming_tool_event_for_path,
)

# Re-export builtin tools
from .gateway_builtin_tools import BUILTIN_TOOLS, GatewayTool, ToolCall, ToolResult

# Re-export new modules
from .gateway_cache import (
    SemanticCache,
    ToolResultCache,
    LocalEmbeddingProvider,
    RemoteEmbeddingProvider,
    cosine_similarity,
    CacheEntry,
    get_semantic_cache,
    get_tool_result_cache,
    reset_caches,
)
from .gateway_claude_compat import (
    CLAUDE_CODE_TOOLS,
    get_claude_code_tool_definitions,
    is_claude_code_tool,
    execute_claude_code_tool,
    format_tool_result_for_anthropic,
    format_tool_use_for_anthropic,
    extract_tool_uses_from_response,
    build_tool_result_message,
)
from .gateway_concurrency import (
    ConcurrencyConfig,
    ConnectionPool,
    LoadBalancer,
    RequestQueue,
    ConcurrentRequestExecutor,
    MultiUpstreamManager,
    create_upstream_pool,
    get_concurrency_stats,
)
from .gateway_intelligence import (
    IntelligenceConfig,
    IntelligenceResult,
    QuestionAnalysis,
    enhance_intelligence,
    refine_answer,
    get_intelligence_summary,
)
from .gateway_stats import (
    StatsConfig,
    RequestStat,
    record_request,
    get_dashboard,
    get_dashboard_json,
    get_top_paths,
    get_top_tools,
    export_stats_csv,
    cleanup_old_stats,
    reset_stats,
)
from .gateway_web2api import (
    Web2ApiEngine,
    get_web2api_engine,
    reset_engine,
)
from .gateway_web_config import (
    ConfigField,
    ConfigTab,
    render_web_config_ui,
    handle_config_post,
    get_config_schema,
)


def _record_tool_failure(*args, **kwargs) -> None:
    """Backward-compatible wrapper for monolithic gateway_app callers."""
    if len(args) >= 2 and isinstance(args[0], ToolCall) and isinstance(args[1], ToolResult):
        call = args[0]
        result = args[1]
        _record_tool_failure_low_level(
            tool_name=result.name or call.name,
            call_id=result.call_id or call.call_id,
            failure_type=result.failure_type,
            arguments_keys=sorted(call.arguments.keys()) if isinstance(call.arguments, dict) else [],
            content=result.content if result.content else "",
            **kwargs,
        )
        return
    _record_tool_failure_low_level(*args, **kwargs)


class _GatewayAppModule(types.ModuleType):
    """Forward legacy gateway_app globals to their owning split modules."""

    _FORWARDED = {
        "CONFIG_PATH": (_gateway_config_module,),
        "REQUEST_LOG_PATH": (_gateway_logging_module,),
        "STATS_PATH": (_gateway_logging_module,),
        "SQLITE_LOG_PATH": (_gateway_logging_module,),
        "SQLITE_READY": (_gateway_logging_module,),
        "_gateway_config": (_gateway_config_module, _gateway_tool_runtime_module),
    }

    def __getattribute__(self, name: str):
        forwarded = types.ModuleType.__getattribute__(self, "_FORWARDED")
        if name in forwarded:
            return getattr(forwarded[name][0], name)
        return types.ModuleType.__getattribute__(self, name)

    def __setattr__(self, name: str, value) -> None:
        forwarded = types.ModuleType.__getattribute__(self, "_FORWARDED")
        if name in forwarded:
            for module in forwarded[name]:
                setattr(module, name, value)
        types.ModuleType.__setattr__(self, name, value)


sys.modules[__name__].__class__ = _GatewayAppModule


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a native tools/function-call runtime gateway")
    parser.add_argument("--host", default=os.environ.get("GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GATEWAY_PORT") or 8885))
    args = parser.parse_args()

    # Load configuration
    config = load_config()
    from .gateway_http_security import validate_bind_security
    exposure_mode = validate_bind_security(args.host, config)

    # Initialize persistence layer
    try:
        from . import gateway_persistence as gp
        persistence_config = config.get("persistence", {})
        gp_config = gp.PersistenceConfig(
            enabled=persistence_config.get("enabled", True),
            db_path=persistence_config.get("db_path", ".gateway_runtime/gateway.db"),
        )
        gp.init_persistence(gp_config)
        print(f"Persistence initialized: {gp_config.db_path}", flush=True)
    except Exception as exc:
        print(f"Warning: Failed to initialize persistence: {exc}", flush=True)

    # Initialize permission system
    try:
        from . import gateway_permissions as gpm
        gpm.init_permissions(config)
        perm_config = config.get("permissions", {})
        if perm_config.get("enabled", False):
            print(f"Tool permissions enabled (default_allow={perm_config.get('default_allow', False)})", flush=True)
        else:
            print("Tool permissions disabled", flush=True)
    except Exception as exc:
        if (config.get("permissions") or {}).get("enabled", False):
            raise RuntimeError("permissions are enabled but failed to initialize") from exc
        print(f"Warning: Failed to initialize disabled permission subsystem: {exc}", flush=True)

    maintenance_stop = threading.Event()

    def maintenance_loop() -> None:
        maintenance_config = config.get("maintenance") or {}
        interval = max(
            30.0,
            float(maintenance_config.get("interval_seconds") or os.environ.get("GATEWAY_MAINTENANCE_INTERVAL_SECONDS") or "300"),
        )
        while not maintenance_stop.wait(interval):
            from .gateway_maintenance import record_maintenance_crash, run_gateway_maintenance
            try:
                snapshot = run_gateway_maintenance(config)
            except Exception as exc:
                snapshot = record_maintenance_crash(exc)
            if not snapshot.get("last_success"):
                print(
                    f"Warning: Gateway maintenance failed: {snapshot.get('last_error') or 'unknown error'}",
                    file=sys.stderr,
                    flush=True,
                )

    maintenance_thread = None
    if bool((config.get("maintenance") or {}).get("enabled", True)):
        maintenance_thread = threading.Thread(target=maintenance_loop, name="gateway-maintenance", daemon=True)
        maintenance_thread.start()

    httpd = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    httpd.daemon_threads = True
    shutdown_started = threading.Event()

    def request_shutdown(signum=None, frame=None) -> None:
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        _set_gateway_ready(False)
        print(f"Shutdown requested{f' by signal {signum}' if signum else ''}", flush=True)
        # BaseServer.shutdown() must run outside the serve_forever thread.
        threading.Thread(target=httpd.shutdown, name="gateway-shutdown", daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
    _set_gateway_ready(True)
    print(f"native tool runtime gateway listening on http://{args.host}:{args.port}", flush=True)
    print(f"public exposure contract: {exposure_mode}", flush=True)
    print("fake prompt tools: disabled", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        _set_gateway_ready(False)
        maintenance_stop.set()
        httpd.server_close()
        try:
            from .gateway_mcp import _mcp_close_sessions
            _mcp_close_sessions()
        except Exception:
            pass
        try:
            from .gateway_builtin_tools import _cleanup_runtime_sessions
            _cleanup_runtime_sessions()
        except Exception:
            pass
        # Clean shutdown
        try:
            from . import gateway_persistence as gp
            gp.close_persistence()
            print("Persistence closed cleanly", flush=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
