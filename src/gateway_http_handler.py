#!/usr/bin/env python3
"""HTTP request handler for the gateway.

Handles HTTP routing, request/response processing, and API endpoints.
"""
from __future__ import annotations

import html
import json
import os
import sys
import traceback
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any

Json = dict[str, Any]

from .gateway_config import SUPPORTED_PATHS, MODEL_LIST_PATHS, TOKEN_COUNT_PATHS, DIRECT_TOOL_CALL_PATHS, _gateway_config, _normalize_request_path, _supported_public_paths, _upstream_config
from .gateway_admin_security import (
    _request_origin,
    _url_origin,
    check_admin_origin as _check_admin_origin_io,
    check_admin_write as _check_admin_write_io,
)
from .gateway_admin_client_mutations import apply_admin_client_mutation
from .gateway_admin_connector_mutations import apply_admin_connector_mutation
from .gateway_admin_config_mutations import apply_admin_config_mutation
from .gateway_errors import error_payload as _error_payload
from .gateway_admin_catalog_mutations import (
    _admin_skill_dir,
    _admin_skill_file,
    _admin_skills_root,
    _safe_admin_skill_name,
    apply_admin_catalog_mutation,
)
from .gateway_http_auth import (
    check_admin as _check_admin_io,
    check_downstream_key as _check_downstream_key_io,
)
from .gateway_http_io import (
    _constant_time_equal,
    _decode_form,
    _decode_json_object,
    _json_response,
    _parse_basic_auth,
    _read_limited_body as _read_limited_body_io,
    _request_body_limit,
    _request_content_length,
    _safe_json_response,
    _text_response,
)
from .gateway_http_security import send_cors_headers


ADMIN_UI_PATHS = {"/ui", "/admin", "/config", "/admin/config-ui"}
_READINESS = threading.Event()


def _set_gateway_ready(ready: bool) -> None:
    if ready:
        _READINESS.set()
    else:
        _READINESS.clear()


def _gateway_is_ready() -> bool:
    return _READINESS.is_set()


def _enforce_request_rate_limit(handler: BaseHTTPRequestHandler, client_id: str | None) -> None:
    from .gateway_config import _gateway_config
    from .gateway_errors import GatewayBusyError, GatewayUnavailableError
    from .gateway_rate_limit import RATE_LIMIT_SERVICE
    cfg = _gateway_config()
    if not cfg.get("rate_limit_enabled", True):
        return
    try:
        rpm = max(1, int(cfg.get("rate_limit_rpm") or 120))
    except (TypeError, ValueError):
        rpm = 120
    remote = str(getattr(handler, "client_address", ("unknown",))[0] or "unknown")
    identity = str(client_id or f"anonymous:{remote}")
    try:
        decision = RATE_LIMIT_SERVICE.consume(identity, rpm, cfg)
    except Exception as exc:
        raise GatewayUnavailableError(
            "request rate-limit backend unavailable",
            detail={"backend": str(cfg.get("rate_limit_backend") or "memory")},
        ) from exc
    if not decision.allowed:
        raise GatewayBusyError(
            f"request rate limit exceeded ({rpm} requests/minute)",
            detail={
                "backend": decision.backend,
                "retry_after_seconds": round(decision.retry_after_seconds, 3),
            },
        )


def _capability_contract() -> Json:
    from .gateway_admission import ADMISSION_SERVICE
    from .gateway_rate_limit import RATE_LIMIT_SERVICE
    from .gateway_http_security import cors_allowed_origins
    from .gateway_sandbox import sandbox_capabilities
    cfg = _gateway_config()
    rate_snapshot = RATE_LIMIT_SERVICE.describe(cfg)
    admission_snapshot = ADMISSION_SERVICE.describe(cfg)
    return {
        "api": {
            "chat_completions": "orchestrated",
            "responses": "orchestrated",
            "messages": "orchestrated",
            "assistants": "minimal_create_compatibility_only",
            "threads": "minimal_create_compatibility_only",
        },
        "request_path": {
            "authenticated_client_rate_limit": True,
            "rate_limit_backend": {
                "configured": rate_snapshot.get("configured_backend"),
                "active": rate_snapshot.get("backend"),
                "degraded": bool(rate_snapshot.get("degraded")),
            },
            "global_request_concurrency_limit": {
                "configured_backend": admission_snapshot.get("configured_backend"),
                "active_backend": admission_snapshot.get("backend"),
                "scope": admission_snapshot.get("scope"),
                "degraded": bool(admission_snapshot.get("degraded")),
            },
            "semantic_cache": "non_streaming_non_tool_exact_request_fingerprint",
            "gateway_concurrency_module": "not_integrated_with_http_request_path",
            "web2api_module": "not_integrated_with_http_request_path",
            "gateway_stats_module": "auxiliary; primary HTTP counters use gateway_logging",
        },
        "security": {
            "cors_enabled": bool(cfg.get("cors_enabled", False)),
            "cors_allowed_origin_count": len(cors_allowed_origins(cfg)),
            "cors_wildcard_allowed": False,
            "request_concurrency_scope": admission_snapshot.get("scope"),
            "execution_isolation": sandbox_capabilities(),
        },
        "operations": {
            "persistent_storage_preflight": "/admin/storage.json",
            "legacy_sqlite_compaction": "offline_cli_only",
            "online_destructive_vacuum": False,
        },
        "observability": {
            "request_id_header": "x-request-id",
            "prometheus_metrics": "/admin/metrics",
            "bounded_trace_ring": "/admin/traces.json",
            "sensitive_metric_labels": False,
            "trace_persistence": False,
        },
        "streaming": {
            "passthrough": "bounded_upstream_sse",
            "orchestrated_safe_text": "end_to_end_incremental",
            "tool_decision_rounds": "upstream_incremental_bounded_until_round_classified",
            "chat_only_rewrite_boundary": "buffered_for_correctness",
            "client_disconnect_cancels_upstream": True,
        },
    }


def _response_contains_tool_request(response: Json) -> bool:
    """Return True when response asks the downstream client to execute tools."""
    if not isinstance(response, dict):
        return False
    for choice in response.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if message.get("tool_calls") or message.get("function_call") or choice.get("finish_reason") == "tool_calls":
            return True
    for block in response.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return True
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") in {"function_call", "web_search_call"}:
            return True
    return False


def _semantic_cache_request_fingerprint(path: str, body: Json) -> str:
    """Return an exact, canonical fingerprint for a cache-eligible request.

    The former key used only the last user text, so different models, system
    prompts, conversation histories, and generation settings could share an
    answer. Keep the runtime scope separate, but bind every JSON request field
    that can affect the response. ``stream`` is omitted because this cache is
    only consulted on the non-streaming path.
    """
    canonical_body = {key: value for key, value in body.items() if key != "stream"}
    payload = json.dumps(
        {"version": 1, "path": _normalize_request_path(path), "body": canonical_body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    import hashlib
    return "gateway-request-v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _agent_runtime_scope_contract() -> Json:
    """Describe which public surfaces are Agent Runtime conversations.

    This contract is intentionally machine-readable for operators and tests:
    supported conversation/tool/public API paths must be covered by the Agent
    Planner or Gateway-owned runtime events, while admin/control-plane, auth
    failures, and unknown paths terminate before any tenant/workspace/session
    can be trusted.
    """
    public_paths = sorted(_supported_public_paths())
    conversation_canonicals = {"/v1/chat/completions", "/v1/messages", "/v1/responses"}
    gateway_owned_canonicals = (
        set(MODEL_LIST_PATHS)
        | set(TOKEN_COUNT_PATHS)
        | set(DIRECT_TOOL_CALL_PATHS)
        | {"/tools/call", "/v1/assistants", "/v1/threads"}
    )
    conversation_paths = sorted(
        path for path in public_paths
        if _normalize_request_path(path) in conversation_canonicals
    )
    gateway_owned_paths = sorted(
        path for path in public_paths
        if _normalize_request_path(path) in gateway_owned_canonicals or path in gateway_owned_canonicals
    )
    return {
        "strict_conversation_scope": "supported_authenticated_public_api_paths",
        "conversation_paths": conversation_paths,
        "gateway_owned_service_paths": gateway_owned_paths,
        "control_plane_paths_excluded": sorted(ADMIN_UI_PATHS | {
            "/",
            "/healthz",
            "/client-config",
            "/client-config.json",
            "/admin/config.json",
            "/admin/stats.json",
            "/admin/requests.json",
            "/admin/failures.json",
            "/admin/memories.json",
            "/admin/agent-planner.json",
            "/admin/agent-capabilities.json",
            "/admin/agent-runtime.json",
            "/admin/agent-runtime-events.json",
            "/admin/agent-runtime-audit.json",
            "/admin/tools.json",
            "/admin/mcp-tools.json",
            "/admin/mcp-health.json",
            "/admin/upstream-models.json",
            "/admin/http-actions.json",
            "/admin/marketplace.json",
            "/admin/skill-create",
            "/admin/skill-install.json",
            "/admin/skill-delete.json",
            "/admin/mcp-install.json",
        }),
        "security_layer_excluded": {
            "auth_failures": "rejected before request body/session metadata is trusted",
            "admin_auth_failures": "admin control plane uses Basic auth and does not create planner sessions",
            "unsupported_paths": "404 before planner because no protocol/workspace/session contract exists",
        },
        "proof_rule": "Use scoped tenant/workspace/session audit for runtime proof; global audit is operator overview only.",
    }


def _agent_runtime_requirement_audit(
    *,
    capabilities: Json,
    sessions: list[Json],
    memories: list[Json],
    events: list[Json],
    filters: Json,
    runtime_config: Json | None = None,
) -> Json:
    """Build a machine-readable Agent Runtime requirement audit.

    The audit is intentionally scoped to the already-filtered runtime data
    passed by the admin handler.  It must not widen tenant/workspace/session
    visibility, because this gateway is a remote multi-user service and the
    client workspace belongs to the caller, not to the gateway process.
    """

    def _event_types(*names: str) -> list[Json]:
        wanted = set(names)
        return [event for event in events if str(event.get("event_type") or "") in wanted]

    def _workflow_events(*workflows: str) -> list[Json]:
        wanted = set(workflows)
        return [event for event in events if str(event.get("workflow") or "") in wanted]

    def _metadata_has(event: Json, key: str, expected: str) -> bool:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        return str(metadata.get(key) or "") == expected

    def _source_values(candidates: list[Json]) -> set[str]:
        values: set[str] = set()
        for event in candidates:
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            source = str(metadata.get("source") or metadata.get("orchestration_source") or "").strip()
            if source:
                values.add(source)
        return values

    def _sample(items: list[Json], *, kind: str, limit: int = 5) -> list[Json]:
        out: list[Json] = []
        for item in items[:limit]:
            out.append({
                "kind": kind,
                "id": item.get("id"),
                "event_type": item.get("event_type"),
                "workflow": item.get("workflow"),
                "step": item.get("step"),
                "tenant_key": item.get("tenant_key"),
                "workspace_key": item.get("workspace_key"),
                "session_key": item.get("session_key") or item.get("memory_session_key"),
                "summary": str(item.get("summary") or "")[:300],
            })
        return out

    def _requirement(
        key: str,
        title: str,
        *,
        runtime_evidence: list[Json] | None = None,
        static_configured: bool = False,
        static_note: str = "",
        detail: Json | None = None,
    ) -> Json:
        runtime_evidence = runtime_evidence or []
        if runtime_evidence:
            status = "proven/current_scope"
        elif static_configured:
            status = "configured/static"
        else:
            status = "missing/current_scope"
        return {
            "key": key,
            "title": title,
            "status": status,
            "evidence_count": len(runtime_evidence),
            "static_configured": bool(static_configured),
            "static_note": static_note,
            "evidence": _sample(runtime_evidence, kind="event"),
            "detail": detail or {},
        }

    workflow_catalog = capabilities.get("workflows") if isinstance(capabilities.get("workflows"), list) else []
    intent_catalog = capabilities.get("intents") if isinstance(capabilities.get("intents"), list) else []
    service_side = capabilities.get("service_side") if isinstance(capabilities.get("service_side"), list) else []
    downstream_owned = capabilities.get("downstream_owned") if isinstance(capabilities.get("downstream_owned"), list) else []
    ownership_model = capabilities.get("ownership_model") if isinstance(capabilities.get("ownership_model"), dict) else {}
    runtime_config = runtime_config if isinstance(runtime_config, dict) else {}
    gateway_mode = str(runtime_config.get("gateway_tool_mode") or "").strip().lower()
    upstream_tools_enabled = str(runtime_config.get("upstream_tools_enabled") or "").strip().lower()
    upstream_supports_tools = bool(runtime_config.get("upstream_supports_tools", False))
    upstream_supports_function_calls = bool(runtime_config.get("upstream_supports_function_calls", False))
    gateway_execute_user_side_tools = bool(runtime_config.get("gateway_execute_user_side_tools", False))
    gateway_delegate_tools_to_downstream = runtime_config.get("gateway_delegate_tools_to_downstream")
    strict_every_turn = bool(runtime_config.get("agent_planner_strict_every_turn", False))
    gateway_forces_local_user_side_tools = gateway_execute_user_side_tools
    upstream_native_tool_authority = (
        upstream_supports_tools
        and upstream_supports_function_calls
        and upstream_tools_enabled not in {"adapter", "text_only", "prompt", "off", "false", "disabled", "none"}
    )
    agent_planner_mode_active = gateway_mode not in {"passthrough", "native_passthrough", "proxy"}

    chat_only_events = _event_types("chat_only_synthesis_boundary", "upstream_tool_attempt_ignored")
    intent_events = _event_types("intent_classification")
    planner_events = _event_types("intent_classification", "planner_state", "tool_dispatch")
    downstream_events = [
        event for event in _event_types("tool_dispatch")
        if _metadata_has(event, "owner", "downstream_client")
        or _metadata_has(event, "dispatch", "downstream_client")
        or str(event.get("workflow") or "") in {"project_analysis", "code_search", "test_build", "fix_loop", "qa_loop", "generic_tool", "edit"}
    ]
    gateway_tool_events = _event_types(
        "gateway_tool_execute",
        "gateway_tool_result",
        "direct_tool_execute",
        "direct_tool_result",
        "token_count_execute",
        "token_count_result",
        "models_result",
        "models_error",
        "assistants_result",
        "assistants_error",
        "threads_result",
        "threads_error",
    )
    rollup_events = _event_types("memory_rollup")
    rollup_memories = [m for m in memories if m.get("kind") == "session_rollup"]
    synthesis_events = _event_types("chat_only_synthesis_boundary")
    synthesis_sources = _source_values(synthesis_events)

    isolation_filters = {
        "tenant_contains": filters.get("tenant_contains"),
        "workspace_contains": filters.get("workspace_contains"),
        "session_contains": filters.get("session_contains"),
    }
    scoped_filter_count = sum(1 for value in isolation_filters.values() if value)
    # Strict per-turn runtime proof is meaningful only for a scoped view.  The
    # unscoped operator dashboard intentionally mixes tenants, anonymous
    # workspaces, old sessions, and bounded event windows; using it as a hard
    # failure signal makes historical pre-strict or aborted anonymous traffic
    # permanently mark the current remote service as broken.  Scoped tenant /
    # workspace / session audits still fail if an intent-classified current
    # session lacks a planner-owned synthesis or tool boundary.
    strict_runtime_scope = scoped_filter_count >= 1
    isolation_evidence = events[:1] if scoped_filter_count >= 2 else []
    parity_evidence = synthesis_events if {"streaming", "non_streaming"}.issubset(synthesis_sources) else []
    session_keys = {str(session.get("session_key") or "") for session in sessions if str(session.get("session_key") or "")}
    event_session_keys = {str(event.get("session_key") or "") for event in events if str(event.get("session_key") or "")}
    intent_session_keys = {str(event.get("session_key") or "") for event in intent_events if str(event.get("session_key") or "")}
    synthesis_session_keys = {str(event.get("session_key") or "") for event in synthesis_events if str(event.get("session_key") or "")}
    dispatch_session_keys = {str(event.get("session_key") or "") for event in downstream_events + gateway_tool_events if str(event.get("session_key") or "")}
    planner_boundary_session_keys = synthesis_session_keys | dispatch_session_keys
    # The session table is durable and may include historical sessions created
    # before strict every-turn mode or outside the current audit evidence
    # window.  Treat only sessions represented by an intent_classification event
    # in the current filtered evidence as auditable; otherwise an unscoped
    # operator view can remain red forever due to stale/truncated sessions even
    # while current traffic is strict.  A session with intent but no planner
    # boundary is still a real strict-mode failure.
    strict_candidate_session_keys = (session_keys & intent_session_keys) if strict_runtime_scope else set()
    strict_covered_session_keys = strict_candidate_session_keys & intent_session_keys & planner_boundary_session_keys
    strict_missing_session_keys = sorted(strict_candidate_session_keys - strict_covered_session_keys)
    strict_every_turn_runtime_evidence = (
        [event for event in intent_events + synthesis_events + downstream_events + gateway_tool_events if str(event.get("session_key") or "") in strict_covered_session_keys]
        if strict_runtime_scope and strict_every_turn and session_keys and not strict_missing_session_keys
        else []
    )

    requirements = {
        "agent_planner_runtime_mode": _requirement(
            "agent_planner_runtime_mode",
            "Runtime is configured to run the outer Agent Planner, not legacy gateway passthrough/proxy mode.",
            runtime_evidence=events[:1] if agent_planner_mode_active and events else [],
            static_configured=agent_planner_mode_active,
            static_note="gateway.tool_mode must not be passthrough/native_passthrough/proxy",
            detail={
                "gateway_tool_mode": gateway_mode or "unknown",
                "upstream_tools_enabled": upstream_tools_enabled or "unknown",
                "legacy_gateway_passthrough": not agent_planner_mode_active,
            },
        ),
        "chat_only_upstream_config": _requirement(
            "chat_only_upstream_config",
            "Active upstream configuration does not grant native tool/function-call authority to the chat-only model.",
            runtime_evidence=events[:1] if events and not upstream_native_tool_authority else [],
            static_configured=not upstream_native_tool_authority,
            static_note="upstream tools must be adapter/text_only/prompt or native tool/function-call support must be disabled",
            detail={
                "upstream_tools_enabled": upstream_tools_enabled or "unknown",
                "upstream_supports_tools": upstream_supports_tools,
                "upstream_supports_function_calls": upstream_supports_function_calls,
                "upstream_native_tool_authority": upstream_native_tool_authority,
            },
        ),
        "downstream_client_tool_execution_policy": _requirement(
            "downstream_client_tool_execution_policy",
            "User-machine tools are configured to execute in the downstream client workspace, not inside the Gateway service.",
            runtime_evidence=downstream_events if not gateway_forces_local_user_side_tools else [],
            static_configured=not gateway_forces_local_user_side_tools,
            static_note="execute_user_side_tools_in_gateway must be false; delegate_tools_to_downstream does not authorize cloud-local user-machine execution",
            detail={
                "gateway_execute_user_side_tools": gateway_execute_user_side_tools,
                "gateway_delegate_tools_to_downstream": gateway_delegate_tools_to_downstream,
                "gateway_forces_local_user_side_tools": gateway_forces_local_user_side_tools,
            },
        ),
        "chat_only_upstream_synthesis_only": _requirement(
            "chat_only_upstream_synthesis_only",
            "Chat-only upstream is used only for final synthesis and has no tool authority.",
            runtime_evidence=chat_only_events,
            static_configured=capabilities.get("chat_only_upstream_role") == "synthesis_only",
            static_note="capability catalog declares chat_only_upstream_role=synthesis_only",
            detail={"tool_authority_granted": False},
        ),
        "planner_owns_intent_and_workflows": _requirement(
            "planner_owns_intent_and_workflows",
            "Remote Agent Planner owns intent classification, workflow registry, state, and dispatch.",
            runtime_evidence=planner_events or _workflow_events("project_analysis", "code_search", "test_build", "fix_loop", "qa_loop"),
            static_configured=bool(workflow_catalog) and bool(intent_catalog),
            static_note="workflow and intent registries are present",
            detail={"workflow_count": len(workflow_catalog), "intent_count": len(intent_catalog), "session_count": len(sessions)},
        ),
        "strict_every_turn_planner_envelope": _requirement(
            "strict_every_turn_planner_envelope",
            "Every communication in strict remote mode is classified by Agent Planner and enters a planner-owned synthesis or tool-dispatch boundary.",
            runtime_evidence=strict_every_turn_runtime_evidence,
            static_configured=strict_every_turn and (not session_keys or not strict_runtime_scope),
            static_note="gateway.agent_planner_strict_every_turn must be true; runtime proof requires tenant/workspace/session scoped sessions with intent_classification plus either chat_only_synthesis_boundary or planner tool_dispatch/gateway_tool events",
            detail={
                "agent_planner_strict_every_turn": strict_every_turn,
                "session_count": len(strict_candidate_session_keys),
                "stored_session_count": len(session_keys),
                "covered_session_count": len(strict_covered_session_keys),
                "synthesis_session_count": len(strict_candidate_session_keys & synthesis_session_keys),
                "dispatch_session_count": len(strict_candidate_session_keys & dispatch_session_keys),
                "missing_session_count": len(strict_missing_session_keys),
                "missing_session_keys": strict_missing_session_keys[:10],
                "runtime_scope_required": True,
                "strict_runtime_scope": strict_runtime_scope,
                "unscoped_intent_session_count": len(session_keys & intent_session_keys) if not strict_runtime_scope else 0,
            },
        ),
        "downstream_client_workspace_tools": _requirement(
            "downstream_client_workspace_tools",
            "Filesystem, shell, GUI, local-agent, and caller-private tools are dispatched to the downstream client workspace.",
            runtime_evidence=downstream_events,
            static_configured=bool(downstream_owned) and "downstream_client" in ownership_model,
            static_note="capability catalog declares downstream_client ownership",
            detail={"downstream_owned_count": len(downstream_owned)},
        ),
        "gateway_owned_service_tools": _requirement(
            "gateway_owned_service_tools",
            "Gateway-owned pure utilities, network/connectors, and direct public endpoints may run service-side before chat-only synthesis.",
            runtime_evidence=gateway_tool_events,
            static_configured=bool(service_side) and "gateway_service" in ownership_model,
            static_note="capability catalog declares gateway_service ownership",
            detail={"service_side_count": len(service_side)},
        ),
        "infinite_context_memory_rollup": _requirement(
            "infinite_context_memory_rollup",
            "Long context is compacted into scoped memory rollups and recalled as evidence.",
            runtime_evidence=rollup_events + rollup_memories,
            static_configured=False,
            detail={"rollup_memory_count": len(rollup_memories), "memory_count": len(memories)},
        ),
        "tenant_workspace_isolation": _requirement(
            "tenant_workspace_isolation",
            "Runtime views are scoped by tenant, workspace, and session so concurrent remote users do not leak state.",
            runtime_evidence=isolation_evidence,
            static_configured=scoped_filter_count == 0 or scoped_filter_count >= 1,
            static_note="admin query filters are applied before audit construction; unscoped audits show configured isolation but scoped tenant/workspace/session filters are required for runtime proof",
            detail={"scoped_filter_count": scoped_filter_count, "filters": isolation_filters},
        ),
        "streaming_nonstreaming_parity": _requirement(
            "streaming_nonstreaming_parity",
            "Streaming and non-streaming orchestration share the same Agent Planner boundaries.",
            runtime_evidence=parity_evidence,
            static_configured=False,
            detail={"seen_synthesis_sources": sorted(synthesis_sources)},
        ),
        "admin_observability": _requirement(
            "admin_observability",
            "Operators can inspect capabilities, planner sessions, memories, runtime events, and this audit surface.",
            runtime_evidence=events[:1],
            static_configured=True,
            static_note="/admin/agent-capabilities.json, /admin/agent-planner.json, /admin/memories.json, /admin/agent-runtime-events.json, /admin/agent-runtime-audit.json",
            detail={"event_count": len(events), "session_count": len(sessions), "memory_count": len(memories)},
        ),
    }
    counts = {"proven": 0, "configured": 0, "missing": 0}
    for item in requirements.values():
        status = str(item.get("status") or "")
        if status.startswith("proven/"):
            counts["proven"] += 1
        elif status.startswith("configured/"):
            counts["configured"] += 1
        else:
            counts["missing"] += 1
    overall = "proven/current_scope" if counts["missing"] == 0 and counts["configured"] == 0 else (
        "partially_proven" if counts["missing"] == 0 else "needs_runtime_evidence"
    )
    return {
        "mode": "remote_agent_planner",
        "scope": filters,
        "runtime_config": {
            "gateway_tool_mode": gateway_mode or "unknown",
            "upstream_tools_enabled": upstream_tools_enabled or "unknown",
            "upstream_supports_tools": upstream_supports_tools,
            "upstream_supports_function_calls": upstream_supports_function_calls,
            "upstream_native_tool_authority": upstream_native_tool_authority,
            "gateway_execute_user_side_tools": gateway_execute_user_side_tools,
            "gateway_delegate_tools_to_downstream": gateway_delegate_tools_to_downstream,
            "gateway_forces_local_user_side_tools": gateway_forces_local_user_side_tools,
            "agent_planner_strict_every_turn": strict_every_turn,
            "legacy_gateway_passthrough": not agent_planner_mode_active,
        },
        "overall_status": overall,
        "summary": {**counts, "total": len(requirements)},
        "requirements": requirements,
        "scope_contract": _agent_runtime_scope_contract(),
    }


def _read_limited_body(handler: BaseHTTPRequestHandler) -> bytes:
    return _read_limited_body_io(
        handler,
        content_length=_request_content_length(handler),
        limit=_request_body_limit(),
    )


def _read_json(handler: BaseHTTPRequestHandler) -> Json:
    return _decode_json_object(_read_limited_body(handler))


def _check_admin(handler: BaseHTTPRequestHandler) -> bool:
    return _check_admin_io(handler, handle_error=_handle_error)


def _check_downstream_key(handler: BaseHTTPRequestHandler) -> str | None:
    return _check_downstream_key_io(handler)


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return _decode_form(
        _read_limited_body(handler),
        content_type=handler.headers.get("Content-Type", ""),
    )


def _check_admin_origin(handler: BaseHTTPRequestHandler, cfg: Json) -> bool:
    return _check_admin_origin_io(
        handler,
        cfg,
        json_response=_json_response,
        error_payload=_error_payload,
    )


def _check_admin_write(handler: BaseHTTPRequestHandler) -> bool:
    return _check_admin_write_io(
        handler,
        check_admin=_check_admin,
        check_origin=_check_admin_origin,
    )


def _redirect(handler: BaseHTTPRequestHandler, location: str = "/ui") -> None:
    handler.send_response(302)
    handler.send_header("Location", location)
    send_cors_headers(handler)
    handler.end_headers()


def _model_ids_from_payload(payload: Any) -> list[str]:
    """Extract model ids from common OpenAI-compatible model-list shapes."""
    models: list[str] = []
    if isinstance(payload, dict):
        candidate_lists: list[Any] = [payload.get("data"), payload.get("models"), payload.get("items")]
    elif isinstance(payload, list):
        candidate_lists = [payload]
    else:
        candidate_lists = []
    for items in candidate_lists:
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
                if model_id:
                    models.append(str(model_id))
            elif isinstance(item, str):
                models.append(item)
    return sorted(dict.fromkeys(models))


def _fetch_upstream_models_for_admin(handler: BaseHTTPRequestHandler, overrides: dict[str, str] | None = None) -> Json:
    """Fetch models from active or form-provided upstream settings for the Admin UI."""
    from .gateway_config import _upstream_config
    from .gateway_errors import GatewayError, UpstreamHTTPError

    upstream_cfg = _upstream_config()
    # GET intentionally ignores query overrides and uses only the saved active
    # profile. Otherwise a cross-site GET in an authenticated browser could make
    # the gateway send the saved upstream Authorization header to an attacker
    # controlled base_url. Temporary discovery overrides are POST-only and pass
    # the Admin Origin/Referer guard before form parsing.
    use_form_overrides = overrides is not None
    overrides = overrides or {}

    def first(name: str) -> str:
        if use_form_overrides and name in overrides and str(overrides.get(name) or "").strip():
            return str(overrides.get(name) or "").strip()
        return ""

    base_url = (first("base_url") or str(upstream_cfg.get("base_url") or "")).rstrip("/")
    # Do not accept temporary API keys from GET query strings: URLs are commonly
    # captured by browser/server logs. The Admin UI submits form overrides via
    # POST body; GET uses the already-saved active profile key.
    api_key = (str(overrides.get("api_key") or "").strip() if use_form_overrides else "") or str(upstream_cfg.get("api_key") or "")
    protocol = first("protocol") or str(upstream_cfg.get("protocol") or "openai_chat")
    paths = upstream_cfg.get("paths") if isinstance(upstream_cfg.get("paths"), dict) else {}
    models_path = first("path_models") or str(paths.get("models") or "/v1/models")
    if not models_path.startswith("/"):
        models_path = "/" + models_path
    if not base_url:
        raise GatewayError("missing upstream base_url")

    headers = {"Accept": "application/json"}
    if api_key:
        if protocol == "anthropic_messages":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base_url}{models_path}"
    try:
        timeout = float(upstream_cfg.get("timeout_seconds") or 60.0)
    except (TypeError, ValueError):
        timeout = 60.0
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UpstreamHTTPError(exc.code, detail) from exc
    models = _model_ids_from_payload(payload)
    return {
        "ok": True,
        "active_model": upstream_cfg.get("model", ""),
        "base_url": base_url,
        "path": models_path,
        "models": models,
        "raw": payload,
    }


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "NativeToolGateway/1.0"

    def handle_one_request(self) -> None:
        from .gateway_observability import begin_request, end_request, observe_request
        started = time.monotonic()
        request_id, root_token = begin_request()
        self._observability_request_id = request_id
        self._observability_header_token = None
        self._observability_status = 0
        try:
            super().handle_one_request()
        finally:
            if getattr(self, "command", None):
                try:
                    observe_request(
                        self.command,
                        getattr(self, "path", ""),
                        int(getattr(self, "_observability_status", 0) or 0),
                        time.monotonic() - started,
                    )
                except Exception:
                    pass
            header_token = getattr(self, "_observability_header_token", None)
            if header_token is not None:
                end_request(header_token)
            end_request(root_token)

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if parsed:
            from .gateway_observability import begin_request
            request_id, token = begin_request(self.headers.get("x-request-id"))
            self._observability_request_id = request_id
            self._observability_header_token = token
        return parsed

    def send_response(self, code: int, message: str | None = None) -> None:
        self._observability_status = int(code)
        super().send_response(code, message)
        request_id = str(getattr(self, "_observability_request_id", "") or "")
        if request_id:
            self.send_header("x-request-id", request_id)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_HEAD(self) -> None:
        path = _normalize_request_path(self.path.split("?", 1)[0])
        if path in {"/", "/healthz", "/livez", "/readyz"} or path in ADMIN_UI_PATHS:
            status = 503 if path in {"/healthz", "/readyz"} and not _gateway_is_ready() else 200
            self.send_response(status)
            self.send_header("content-type", "text/plain; charset=utf-8")
            send_cors_headers(self)
            self.end_headers()
            return
        self.send_response(404)
        send_cors_headers(self)
        self.end_headers()

    def do_GET(self) -> None:
        path = _normalize_request_path(self.path.split("?", 1)[0])
        if path == "/":
            _text_response(
                self,
                200,
                "Tool Call Gateway is running.\n\nAPI: /v1/messages, /v1/chat/completions, /v1/responses\nHealth: /healthz\nAdmin UI: /ui (basic auth)\nClient config: /client-config\n",
                "text/plain; charset=utf-8",
            )
            return
        if path in {"/healthz", "/livez", "/readyz"}:
            from .gateway_builtin_tools import BUILTIN_TOOLS
            ready = _gateway_is_ready()
            if path in {"/healthz", "/readyz"} and not ready:
                _json_response(self, 503, {"ok": False, "live": True, "ready": False})
                return
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "live": True,
                    "ready": ready,
                    "mode": os.environ.get("GATEWAY_TOOL_MODE", "orchestrate"),
                    "fake_prompt_tools": False,
                    "supported_paths": sorted(_supported_public_paths()),
                    "builtin_tool_count": len({tool.name for tool in BUILTIN_TOOLS.values()}),
                },
            )
            return
        if path == "/capabilities":
            _json_response(self, 200, _capability_contract())
            return
        from .gateway_admin_operations import handle_admin_operations_get
        if handle_admin_operations_get(
            self,
            path,
            check_admin=_check_admin,
            json_response=_json_response,
            text_response=_text_response,
            ready=_gateway_is_ready,
        ):
            return
        if path in MODEL_LIST_PATHS:
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            downstream_key = None
            runtime_body: Json = {"metadata": {"session_id": query.get("session_id", ["models"])[0] or "models"}}
            try:
                downstream_key = _check_downstream_key(self)
                _enforce_request_rate_limit(self, downstream_key)
                from .gateway_tool_runtime import _request_slot_scope
                with _request_slot_scope():
                    metadata = runtime_body.setdefault("metadata", {})
                    tenant = query.get("tenant", [""])[0] or query.get("user_id", [""])[0] or downstream_key or "anonymous"
                    metadata["tenant"] = tenant
                    metadata["user_id"] = query.get("user_id", [""])[0] or tenant
                    workspace = query.get("workspace", [""])[0] or query.get("workspace_root", [""])[0]
                    if workspace:
                        metadata["workspace"] = workspace
                    from .gateway_proxy import NativeProxyClient
                    response = NativeProxyClient().get(path)
                    from .gateway_tool_runtime import record_gateway_public_endpoint
                    record_gateway_public_endpoint(path, runtime_body, resource="models", action="list", response=response, client_id=downstream_key)
                    from .gateway_logging import _record_request_stat, _write_request_log
                    _record_request_stat(path, 200)
                    _write_request_log(path, {}, 200, response, downstream_key)
                    _json_response(self, 200, response)
            except Exception as exc:
                try:
                    from .gateway_tool_runtime import record_gateway_public_endpoint
                    if downstream_key:
                        runtime_body.setdefault("metadata", {}).setdefault("tenant", downstream_key)
                    record_gateway_public_endpoint(path, runtime_body, resource="models", action="list", success=False, failure_type=type(exc).__name__, client_id=downstream_key)
                except Exception:
                    pass
                _handle_error(self, path, exc)
            return
        if path in ADMIN_UI_PATHS:
            if not _check_admin(self):
                return
            from .gateway_admin import _render_admin_ui
            _text_response(self, 200, _render_admin_ui())
            return
        if path == "/client-config":
            if not _check_admin(self):
                return
            from .gateway_admin import _render_client_config_ui
            _text_response(self, 200, _render_client_config_ui())
            return
        if path == "/client-config.json":
            if not _check_admin(self):
                return
            from .gateway_admin import _client_config_snippets
            _json_response(self, 200, _client_config_snippets())
            return
        if path == "/admin/config.json":
            if not _check_admin(self):
                return
            from .gateway_config import load_config, _redacted_config
            _json_response(self, 200, {"config": _redacted_config(load_config())})
            return
        if path == "/admin/memories.json":
            if not _check_admin(self):
                return
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["200"])[0])
            except (TypeError, ValueError):
                limit = 200

            def _query_text(name: str) -> str | None:
                val = query.get(name, [""])[0]
                val = str(val or "").strip()
                return val or None

            def _query_bool(name: str) -> bool | None:
                if name not in query:
                    return None
                val = str(query.get(name, [""])[0] or "").strip().lower()
                if val in {"1", "true", "yes", "y", "on"}:
                    return True
                if val in {"0", "false", "no", "n", "off"}:
                    return False
                return None

            filters = {
                "tenant_contains": _query_text("tenant_contains"),
                "workspace_contains": _query_text("workspace_contains"),
                "session_contains": _query_text("session_contains"),
                "kind": _query_text("kind"),
                "has_rollup": _query_bool("has_rollup"),
            }
            from .gateway_context import _sqlite_tail_memories
            memories = _sqlite_tail_memories(limit, **filters)
            _json_response(self, 200, {"memories": memories, "filters": filters, "limit": max(1, min(limit, 500))})
            return
        if path == "/admin/agent-planner.json":
            if not _check_admin(self):
                return
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                limit = 50

            def _query_text(name: str) -> str | None:
                val = query.get(name, [""])[0]
                val = str(val or "").strip()
                return val or None

            def _query_bool(name: str) -> bool | None:
                if name not in query:
                    return None
                val = str(query.get(name, [""])[0] or "").strip().lower()
                if val in {"1", "true", "yes", "y", "on"}:
                    return True
                if val in {"0", "false", "no", "n", "off"}:
                    return False
                return None

            from .gateway_agent_planner import _store
            filters = {
                "workflow": _query_text("workflow"),
                "current_step": _query_text("current_step"),
                "session_contains": _query_text("session_contains"),
                "tenant_contains": _query_text("tenant_contains"),
                "workspace_contains": _query_text("workspace_contains"),
                "has_evidence": _query_bool("has_evidence"),
            }
            sessions = _store().list_recent(limit, **filters)
            _json_response(self, 200, {"sessions": sessions, "filters": filters, "limit": max(1, min(limit, 500))})
            return
        if path == "/admin/agent-capabilities.json":
            if not _check_admin(self):
                return
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            include_mcp_tools = str(query.get("include_mcp_tools", ["0"])[0] or "").strip().lower() in {"1", "true", "yes", "on"}
            from .gateway_tool_runtime import planner_capability_catalog
            _json_response(self, 200, planner_capability_catalog(include_mcp_tools=include_mcp_tools))
            return
        if path == "/admin/agent-runtime.json":
            if not _check_admin(self):
                return
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            limit = max(1, min(limit, 500))

            def _query_text(name: str) -> str | None:
                val = query.get(name, [""])[0]
                val = str(val or "").strip()
                return val or None

            def _query_bool(name: str) -> bool | None:
                if name not in query:
                    return None
                val = str(query.get(name, [""])[0] or "").strip().lower()
                if val in {"1", "true", "yes", "y", "on"}:
                    return True
                if val in {"0", "false", "no", "n", "off"}:
                    return False
                return None

            tenant_contains = _query_text("tenant_contains")
            session_contains = _query_text("session_contains")
            workflow = _query_text("workflow")
            current_step = _query_text("current_step")
            workspace_contains = _query_text("workspace_contains")
            memory_kind = _query_text("memory_kind") or _query_text("kind")
            has_evidence = _query_bool("has_evidence")
            has_rollup = _query_bool("has_rollup")
            event_type = _query_text("event_type")

            from .gateway_agent_planner import _store, list_runtime_events
            from .gateway_context import _sqlite_tail_memories
            from .gateway_tool_runtime import planner_capability_catalog
            planner_filters = {
                "workflow": workflow,
                "current_step": current_step,
                "session_contains": session_contains,
                "tenant_contains": tenant_contains,
                "workspace_contains": workspace_contains,
                "has_evidence": has_evidence,
            }
            memory_filters = {
                "tenant_contains": tenant_contains,
                "workspace_contains": workspace_contains,
                "session_contains": session_contains,
                "kind": memory_kind,
                "has_rollup": has_rollup,
            }
            sessions = _store().list_recent(limit, **planner_filters)
            memories = _sqlite_tail_memories(limit, **memory_filters)
            rollups = [m for m in memories if m.get("kind") == "session_rollup"]
            events = list_runtime_events(
                limit,
                tenant_contains=tenant_contains,
                workspace_contains=workspace_contains,
                session_contains=session_contains,
                event_type=event_type,
                workflow=workflow,
                step=current_step,
            )
            active_workflows = sorted({str(s.get("workflow") or "") for s in sessions if s.get("workflow")})
            _json_response(self, 200, {
                "runtime": {
                    "agent_planner": {
                        "sessions": sessions,
                        "session_count": len(sessions),
                        "active_workflows": active_workflows,
                    },
                    "memory": {
                        "memories": memories,
                        "memory_count": len(memories),
                        "rollup_count": len(rollups),
                    },
                    "events": {
                        "items": events,
                        "event_count": len(events),
                    },
                    "capabilities": planner_capability_catalog(include_mcp_tools=False),
                },
                "filters": {
                    "tenant_contains": tenant_contains,
                    "workspace_contains": workspace_contains,
                    "session_contains": session_contains,
                    "workflow": workflow,
                    "current_step": current_step,
                    "memory_kind": memory_kind,
                    "event_type": event_type,
                    "has_evidence": has_evidence,
                    "has_rollup": has_rollup,
                },
                "limit": limit,
            })
            return
        if path == "/admin/agent-runtime-audit.json":
            if not _check_admin(self):
                return
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["200"])[0])
            except (TypeError, ValueError):
                limit = 200
            limit = max(1, min(limit, 500))
            try:
                audit_limit = int(query.get("audit_limit", ["500"])[0])
            except (TypeError, ValueError):
                audit_limit = 500
            audit_limit = max(limit, min(max(1, audit_limit), 500))

            def _query_text(name: str) -> str | None:
                val = query.get(name, [""])[0]
                val = str(val or "").strip()
                return val or None

            tenant_contains = _query_text("tenant_contains")
            workspace_contains = _query_text("workspace_contains")
            session_contains = _query_text("session_contains")
            workflow = _query_text("workflow")
            current_step = _query_text("current_step")
            event_type = _query_text("event_type")
            memory_kind = _query_text("memory_kind") or _query_text("kind")

            from .gateway_agent_planner import _store, list_runtime_events
            from .gateway_context import _sqlite_tail_memories
            from .gateway_tool_runtime import planner_capability_catalog

            planner_filters = {
                "workflow": workflow,
                "current_step": current_step,
                "session_contains": session_contains,
                "tenant_contains": tenant_contains,
                "workspace_contains": workspace_contains,
                "has_evidence": None,
            }
            memory_filters = {
                "tenant_contains": tenant_contains,
                "workspace_contains": workspace_contains,
                "session_contains": session_contains,
                "kind": memory_kind,
                "has_rollup": None,
            }
            event_filters = {
                "tenant_contains": tenant_contains,
                "workspace_contains": workspace_contains,
                "session_contains": session_contains,
                "event_type": event_type,
                "workflow": workflow,
                "step": current_step,
            }
            sessions = _store().list_recent(audit_limit, **planner_filters)
            memories = _sqlite_tail_memories(audit_limit, **memory_filters)
            events = list_runtime_events(audit_limit, **event_filters)
            gateway_cfg = _gateway_config()
            upstream_cfg = _upstream_config()
            upstream_caps = upstream_cfg.get("capabilities") if isinstance(upstream_cfg.get("capabilities"), dict) else {}
            filters = {
                "tenant_contains": tenant_contains,
                "workspace_contains": workspace_contains,
                "session_contains": session_contains,
                "workflow": workflow,
                "current_step": current_step,
                "memory_kind": memory_kind,
                "event_type": event_type,
            }
            audit = _agent_runtime_requirement_audit(
                capabilities=planner_capability_catalog(include_mcp_tools=False),
                sessions=sessions,
                memories=memories,
                events=events,
                filters=filters,
                runtime_config={
                    "gateway_tool_mode": (gateway_cfg.get("tool_mode") if isinstance(gateway_cfg, dict) else ""),
                    "agent_planner_strict_every_turn": bool(gateway_cfg.get("agent_planner_strict_every_turn", False)) if isinstance(gateway_cfg, dict) else False,
                    "gateway_execute_user_side_tools": bool(gateway_cfg.get("execute_user_side_tools_in_gateway", False)) if isinstance(gateway_cfg, dict) else False,
                    "gateway_delegate_tools_to_downstream": gateway_cfg.get("delegate_tools_to_downstream") if isinstance(gateway_cfg, dict) and "delegate_tools_to_downstream" in gateway_cfg else None,
                    "upstream_tools_enabled": (upstream_cfg.get("tools_enabled") if isinstance(upstream_cfg, dict) else ""),
                    "upstream_supports_tools": bool(upstream_caps.get("supports_tools", False)),
                    "upstream_supports_function_calls": bool(upstream_caps.get("supports_function_calls", False)),
                },
            )
            _json_response(self, 200, {
                "audit": audit,
                "inputs": {
                    "session_count": len(sessions),
                    "memory_count": len(memories),
                    "event_count": len(events),
                },
                "filters": filters,
                "limit": limit,
                "audit_limit": audit_limit,
            })
            return
        if path == "/admin/agent-runtime-events.json":
            if not _check_admin(self):
                return
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["100"])[0])
            except (TypeError, ValueError):
                limit = 100

            def _query_text(name: str) -> str | None:
                val = query.get(name, [""])[0]
                val = str(val or "").strip()
                return val or None

            from .gateway_agent_planner import list_runtime_events
            filters = {
                "tenant_contains": _query_text("tenant_contains"),
                "workspace_contains": _query_text("workspace_contains"),
                "session_contains": _query_text("session_contains"),
                "event_type": _query_text("event_type"),
                "workflow": _query_text("workflow"),
                "step": _query_text("step") or _query_text("current_step"),
            }
            events = list_runtime_events(limit, **filters)
            _json_response(self, 200, {"events": events, "filters": filters, "limit": max(1, min(limit, 500))})
            return
        if path == "/admin/tools.json":
            if not _check_admin(self):
                return
            from .gateway_logging import _tool_catalog_snapshot
            _json_response(self, 200, _tool_catalog_snapshot())
            return
        if path == "/admin/mcp-tools.json":
            if not _check_admin(self):
                return
            from .gateway_mcp import _enabled_mcp_servers, _mcp_list_server_tools, _mcp_public_name
            tools: list[Json] = []
            for server in _enabled_mcp_servers():
                server_name = str(server.get("name") or "")
                try:
                    for tool in _mcp_list_server_tools(server):
                        tools.append(
                            {
                                "server": server_name,
                                "name": tool.get("name"),
                                "gateway_name": _mcp_public_name(server_name, str(tool.get("name"))),
                                "description": tool.get("description"),
                            }
                        )
                except Exception as exc:
                    tools.append({"server": server_name, "error": str(exc)})
            _json_response(self, 200, {"tools": tools})
            return
        if path == "/admin/mcp-health.json":
            if not _check_admin(self):
                return
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            probe = query.get("probe", ["0"])[0] in {"1", "true", "yes"}
            from .gateway_mcp import _mcp_health_snapshot
            _json_response(self, 200, {"servers": _mcp_health_snapshot(probe=probe)})
            return
        if path == "/admin/upstream-models.json":
            if not _check_admin(self):
                return
            try:
                _json_response(self, 200, _fetch_upstream_models_for_admin(self))
            except Exception as exc:
                _handle_error(self, path, exc)
            return
        if path == "/admin/http-actions.json":
            if not _check_admin(self):
                return
            from .gateway_http_actions import _enabled_http_actions
            actions = []
            for action in _enabled_http_actions():
                actions.append(
                    {
                        "name": action.get("name"),
                        "method": str(action.get("method") or "POST").upper(),
                        "url": action.get("url"),
                        "description": action.get("description"),
                        "enabled": action.get("enabled", True),
                    }
                )
            _json_response(self, 200, {"actions": actions})
            return
        if path == "/admin/marketplace.json":
            if not _check_admin(self):
                return
            try:
                from .marketplace import list_mcp_marketplace, list_skills_catalog, scan_local_skills
                mcp_items = list_mcp_marketplace()
                skills = list_skills_catalog()
                local_skills = scan_local_skills()
                _json_response(self, 200, {
                    "mcp_servers": mcp_items,
                    "skills": skills,
                    "local_skills": local_skills,
                })
            except Exception as exc:
                _json_response(self, 200, {"error": str(exc), "mcp_servers": [], "skills": [], "local_skills": []})
            return
        _json_response(self, 404, _error_payload("not found"))

    def do_POST(self) -> None:
        try:
            path = _normalize_request_path(self.path.split("?", 1)[0])
            if path == "/admin/upstream-models.json":
                if not _check_admin(self):
                    return
                from .gateway_config import load_config
                cfg = load_config()
                if not _check_admin_origin(self, cfg):
                    return
                form = _read_form(self)
                try:
                    _json_response(self, 200, _fetch_upstream_models_for_admin(self, form))
                except Exception as exc:
                    _handle_error(self, path, exc)
                return
            if path in {"/admin/config", "/admin/upstream-profile", "/admin/client-config", "/admin/password", "/admin/downstream-key", "/admin/mcp", "/admin/mcp-reload", "/admin/http-actions"}:
                if not _check_admin(self):
                    return
                from .gateway_config import load_config_with_revision, save_config
                cfg, config_revision = load_config_with_revision()
                if not _check_admin_origin(self, cfg):
                    return
                form = _read_form(self)
                client_mutation = apply_admin_client_mutation(
                    path,
                    cfg,
                    config_revision,
                    form,
                )
                if client_mutation.matched:
                    if not client_mutation.success:
                        _json_response(
                            self,
                            client_mutation.status,
                            _error_payload(client_mutation.error),
                        )
                        return
                connector_mutation = apply_admin_connector_mutation(
                    path,
                    cfg,
                    config_revision,
                    form,
                )
                if connector_mutation.matched:
                    if not connector_mutation.success:
                        _json_response(
                            self,
                            connector_mutation.status,
                            _error_payload(connector_mutation.error),
                        )
                        return
                config_mutation = apply_admin_config_mutation(
                    path,
                    cfg,
                    config_revision,
                    form,
                )
                if config_mutation.matched and not config_mutation.success:
                    _json_response(
                        self,
                        config_mutation.status,
                        _error_payload(config_mutation.error),
                    )
                    return
                _redirect(self, "/ui")
                return
            if path in SUPPORTED_PATHS or path in TOKEN_COUNT_PATHS or path in DIRECT_TOOL_CALL_PATHS:
                downstream_key = _check_downstream_key(self)
                _enforce_request_rate_limit(self, downstream_key)
                from .gateway_tool_runtime import _request_slot_scope
                with _request_slot_scope():
                    body = _read_json(self)

                    if path in TOKEN_COUNT_PATHS:
                        from .gateway_tool_runtime import token_count_response
                        response = token_count_response(body, path=path, client_id=downstream_key)
                        from .gateway_logging import _record_request_stat, _write_request_log
                        _record_request_stat(path, 200)
                        _write_request_log(path, body, 200, response, downstream_key)
                        _json_response(self, 200, response)
                        return
                    if path in DIRECT_TOOL_CALL_PATHS:
                        from .gateway_tool_runtime import execute_direct_tool_call
                        response = execute_direct_tool_call(body, path=path, client_id=downstream_key)
                        from .gateway_logging import _record_request_stat, _write_request_log
                        _record_request_stat(path, 200)
                        _write_request_log(path, body, 200, response, downstream_key)
                        _json_response(self, 200, response)
                        return
                    from .gateway_assistants import handle_assistants_or_threads
                    gateway_owned_response = handle_assistants_or_threads(path, body)
                    if gateway_owned_response is not None:
                        from .gateway_tool_runtime import record_gateway_public_endpoint
                        record_gateway_public_endpoint(
                            path,
                            body,
                            resource="assistants" if path == "/v1/assistants" else "threads",
                            action="create",
                            response=gateway_owned_response,
                            client_id=downstream_key,
                        )
                        from .gateway_logging import _record_request_stat, _write_request_log
                        _record_request_stat(path, 200)
                        _write_request_log(path, body, 200, gateway_owned_response, downstream_key)
                        _json_response(self, 200, gateway_owned_response)
                        return
                    stream = body.get("stream", False)
                    if stream:
                        from .gateway_streaming import run_streaming_orchestration
                        run_streaming_orchestration(self, path, body, client_id=downstream_key)
                    else:
                        # Check semantic cache for non-streaming requests
                        cache_hit = None
                        query_text = ""
                        semantic_cache_scope = ""
                        try:
                            from .gateway_cache import get_semantic_cache
                            from .gateway_tool_runtime import _request_runtime_scope_key, _request_scope_body, _request_workspace_root
                            cache = get_semantic_cache()
                            # Tool requests are schema-dependent and often
                            # multi-turn.  Reusing a cached tool_use/tool_calls
                            # response can send stale names/arguments to real
                            # clients (Claude Code/Codex), so cache only plain
                            # assistant answers.
                            #
                            # In strict remote Agent Planner mode every
                            # communication must pass through the planner so
                            # intent, session/workspace isolation, audit events,
                            # and synthesis boundaries are recorded for that
                            # exact turn.  A semantic-cache hit here bypasses
                            # run_tool_orchestration entirely and can return an
                            # old response with another path/session's planner
                            # context, so strict mode must not use this cache.
                            from .gateway_agent_planner import strict_agent_planner_every_turn
                            cache_allowed = (
                                not strict_agent_planner_every_turn()
                                and not body.get("tools")
                                and not body.get("tool_choice")
                            )
                            query_text = _semantic_cache_request_fingerprint(path, body) if cache_allowed else ""
                            if query_text:
                                scoped_body = _request_scope_body(body, downstream_key)
                                scoped_root = _request_workspace_root(scoped_body)
                                semantic_cache_scope = _request_runtime_scope_key(scoped_body, scoped_root)
                                cache_hit = cache.get(query_text, scope_key=semantic_cache_scope)
                                if _response_contains_tool_request(cache_hit or {}):
                                    cache_hit = None
                        except Exception:
                            cache_hit = None
                            query_text = ""
                            semantic_cache_scope = ""

                        if cache_hit is not None:
                            from .gateway_logging import _record_request_stat, _write_request_log
                            _record_request_stat(path, 200)
                            _write_request_log(path, body, 200, cache_hit, downstream_key)
                            _json_response(self, 200, cache_hit)
                        else:
                            from .gateway_tool_runtime import run_tool_orchestration
                            # Pass client_id for permission checking (use downstream_key as client identifier)
                            response = run_tool_orchestration(path, body, client_id=downstream_key)
                            # Store in semantic cache if eligible
                            try:
                                if query_text and semantic_cache_scope and cache_hit is None and not _response_contains_tool_request(response):
                                    cache.put(query_text, response, scope_key=semantic_cache_scope)
                            except Exception:
                                pass
                            from .gateway_logging import _record_request_stat, _write_request_log
                            _record_request_stat(path, 200)
                            _write_request_log(path, body, 200, response, downstream_key)
                            _json_response(self, 200, response)
                return
            if path in {
                "/admin/skill-create",
                "/admin/skill-install.json",
                "/admin/skill-delete.json",
                "/admin/mcp-install.json",
            }:
                if not _check_admin_write(self):
                    return
                payload = _read_form(self) if path == "/admin/skill-create" else _read_json(self)
                result = apply_admin_catalog_mutation(path, payload)
                if result.redirect:
                    _redirect(self, result.redirect)
                else:
                    _json_response(self, result.status, result.payload)
                return
            _json_response(self, 404, _error_payload("not found"))
        except Exception as exc:
            _handle_error(self, _normalize_request_path(self.path.split("?", 1)[0]), exc)

    def do_OPTIONS(self) -> None:
        from .gateway_http_security import cors_origin_allowed
        if self.headers.get("Origin") and not cors_origin_allowed(self):
            _json_response(self, 403, _error_payload("CORS origin is not allowed"))
            return
        self.send_response(204)
        send_cors_headers(self, preflight=True)
        self.end_headers()


def _handle_error(handler: BaseHTTPRequestHandler, path: str, exc: Exception) -> None:
    from .gateway_errors import GatewayError, UpstreamHTTPError, DownstreamAuthError, GatewayBusyError
    from .gateway_logging import _record_request_stat
    def record_status(status: int) -> None:
        try:
            _record_request_stat(path, status)
        except Exception:
            pass

    if isinstance(exc, UpstreamHTTPError):
        record_status(502)
        _safe_json_response(handler, 502, _error_payload(str(exc), detail=exc.detail, upstream_status=exc.upstream_status))
    elif isinstance(exc, DownstreamAuthError):
        record_status(401)
        _safe_json_response(handler, 401, _error_payload(str(exc)))
    elif isinstance(exc, GatewayBusyError):
        record_status(429)
        retry_after = 1
        if isinstance(exc.detail, dict):
            try:
                retry_after = max(1, int(float(exc.detail.get("retry_after_seconds") or 0) + 0.999))
            except (TypeError, ValueError, OverflowError):
                retry_after = 1
        _safe_json_response(
            handler,
            429,
            _error_payload(str(exc), detail=exc.detail),
            headers={"Retry-After": str(retry_after)},
        )
    elif isinstance(exc, GatewayError):
        record_status(exc.status)
        _safe_json_response(handler, exc.status, _error_payload(str(exc), detail=exc.detail))
    else:
        if os.environ.get("DEBUG"):
            traceback.print_exc()
        record_status(500)
        _safe_json_response(handler, 500, _error_payload(str(exc)))
