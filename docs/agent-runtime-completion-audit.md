# Agent Runtime Completion Audit

本审计文件按用户目标拆分当前完成度：把原 gateway 调整为远端外层 Agent Planner / Agent Runtime；chat-only upstream 只处理对话综合；所有功能由 Planner/Runtime 进行意图解析、调度、工具分派、证据收集和长上下文记忆。

## Requirements and current evidence

| Requirement | Current status | Evidence |
| --- | --- | --- |
| Runtime is Agent Planner, not legacy gateway passthrough | Proven in scoped smoke | `/admin/agent-runtime-audit.json` requirement `agent_planner_runtime_mode=proven/current_scope`; passthrough regression test forces `missing/current_scope` |
| Chat-only upstream only synthesizes final text | Proven in scoped smoke | `chat_only_upstream_config=proven/current_scope`; `chat_only_synthesis_boundary`, `tool_authority_granted=false`, `upstream_tool_attempt_ignored`; native upstream authority regression is flagged |
| Agent Planner owns intent/workflow/dispatch | Proven in scoped smoke | `INTENT_REGISTRY`, `WORKFLOW_REGISTRY`, `intent_classification`, `tool_dispatch`, project-analysis smoke |
| User-machine tools run in downstream client workspace | Proven in scoped smoke | `downstream_client_tool_execution_policy=proven/current_scope`; remote pressure smoke checks `Read` paths only within per-user client workspaces and no service-root leak; Gateway-side user-tool execution regression is flagged |
| Gateway-owned service tools can execute service-side | Proven in scoped smoke | Remote pressure smoke triggers Gateway-owned `calculator`; runtime events `gateway_tool_execute/result` |
| Infinite context via periodic summary/recall/reinjection | Proven in pressure smoke | Long-context smoke: 4 users, rollups checked, streaming Responses recall checked, compaction checked, no cross-tenant leak |
| Multi-tenant concurrent isolation | Proven in pressure smoke | 6-user remote pressure smoke; tenant/workspace/session filters for runtime/memory/events/audit |
| Streaming/non-streaming parity | Proven in pressure smoke | Same admin scope records `chat_only_synthesis_boundary` with `source=non_streaming` and `source=streaming` |
| Operator observability | Proven in scoped smoke | `/admin/agent-capabilities.json`, `/admin/agent-planner.json`, `/admin/agent-runtime.json`, `/admin/agent-runtime-events.json`, `/admin/agent-runtime-audit.json` |

## Latest targeted verification

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode -q
# 2 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true
```

## Remaining audit posture

The implementation now has runtime evidence for the main target semantics. Before declaring the whole thread goal complete, keep running the full verification gate after each final change:

```bash
python3 -m pytest -q
python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/project_scope_cli_smoke.py
python3 tests/integration/agent_planner_remote_pressure_smoke.py
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
git diff --check
grep -RIn --exclude-dir=.git --exclude-dir=.gateway_runtime --exclude='.gateway_service.json' --exclude='.case.txt' '<redacted-real-key>' . 2>/dev/null || true
```

## Final verification gate 2026-06-26

完整目标验收已通过。命令与证据：

```bash
python3 -m pytest -q
# 965 passed, 2 skipped, 21 warnings in 51.35s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=1; ignored_upstream_tool_attempt=Edit

python3 tests/integration/project_scope_cli_smoke.py
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; rollups_checked=4; streaming_responses_recall_checked=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

Final conclusion: current evidence proves the requested end state for this repository snapshot: the service is operating as a remote Agent Planner / Agent Runtime with chat-only upstream synthesis, planner-owned intent/workflow/tool dispatch, downstream-client workspace isolation for user-machine tools, Gateway-owned service tools, scoped multi-tenant memory rollup/recall, streaming/non-streaming parity, and admin audit surfaces that flag legacy gateway/native/upstream-tool escape hatches.


## Live client-context poisoning regression — 2026-06-27

新增完成项：真实客户端会把 `<system-reminder>`、`PreToolUse`、`SessionStart`、AGENTS/CLAUDE 指令等作为 user-block 附带上传。此前 sanitizer 在文本开头看到 `<system-reminder>` 时直接返回空，且 recalled memory block 的多行摘要会吞掉后续真实当前指令，导致：

1. `jo` 可能被历史 PreToolUse/test runner 污染；
2. 同一 session 下一轮 `分析这套项目` 被误判为 `plain_chat`；
3. chat-only upstream 看到 `/Users/sanbo/Desktop/ti` 等旧 workspace 内容并输出“无法访问文件/换话题”。

修复后，Planner 与 final synthesis 使用一致的净化用户文本；recalled memory 只作为证据，不覆盖当前用户指令；memory 注入摘要改为单行，减少跨轮污染面。

Live 证据：

```text
jo with injected client context -> stop=end_turn, tools=[], intent=plain_chat, workflow=chat_only_synthesis
分析这套项目 same session -> stop=tool_use, tools=[Bash], intent=project_analysis, workflow=project_analysis
workspace=/Users/sanbo/Desktop/ai_tool_functioncall
```

回归命令：

```bash
python3 -m pytest -q tests/test_agent_planner_client_context.py
python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_protocol_strict_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
```


## Audit-window correction — 2026-06-27

Completion evidence now distinguishes current scoped proof from historical unscoped operator views. The default audit endpoint collects up to `audit_limit=500` filtered records and strict every-turn coverage only evaluates sessions that have current-window `intent_classification` evidence. This prevents old anonymous sessions from permanently marking the current runtime as missing while preserving real failures: an intent-classified session without a planner-owned boundary is still missing.

Latest verification additions:

```bash
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_ignores_stale_sessions_outside_event_window
python3 tests/integration/agent_planner_remote_pressure_smoke.py
python3 tests/integration/agent_planner_public_surface_smoke.py
python3 tests/integration/agent_planner_protocol_strict_smoke.py
```

## Live user transcript re-test — 2026-06-27

User-reported failure chain was re-tested against the running service on `127.0.0.1:8885` after the client-context poisoning and audit-window fixes.

Evidence:

```text
/v1/messages?beta=true, workspace=/Users/sanbo/Desktop/ti, same session:
  user: jo
  result: stop_reason=end_turn; no tool_use; intent=plain_chat

  user: 分析这套项目
  result: stop_reason=tool_use; tool=Bash; intent=project_analysis

/v1/chat/completions, same session:
  user: jo
  result: finish_reason=stop; tool_calls=null

  user: 分析这套项目
  result: finish_reason=tool_calls; function=Bash

Tool-result continuation:
  project_structure Bash result returned to Gateway
  result: planner continued with core_flow_trace Bash instead of handing the turn to chat-only upstream refusal
```

Live smoke gate on the same service:

```bash
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; admin_audit_checked=true

python3 tests/integration/agent_planner_public_surface_smoke.py
# ok=true; advertised_count=21; strict missing_session_count=0

python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; covered_session_count=12; missing_session_count=0

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; cross_tenant_leak_checked=true; compacted=true
```

Conclusion for this evidence window: the current process satisfies the reported client path. If the same symptom appears again, treat it as environment/routing drift first: verify active PID, endpoint, client API key profile, tenant/workspace/session metadata, and admin runtime events for that exact request.

## Global audit strict false-positive closure — 2026-06-27

A completion-audit gap remained after the client-context fix: unscoped global audit could still report `strict_every_turn_planner_envelope=missing/current_scope` because the durable DB contained historical anonymous sessions whose event window had `intent_classification` but no later `chat_only_synthesis_boundary` instrumentation.

The audit now separates **operator overview** from **runtime proof**:

- Global/unscoped audit: reports strict mode as `configured/static`, exposes `unscoped_intent_session_count`, and does not fail current completion on historical anonymous sessions.
- Scoped audit: still provides hard runtime proof and still fails if a scoped current session lacks a planner boundary.

Regression and live checks:

```bash
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_global_view_does_not_fail_on_unscoped_historical_anonymous_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_ignores_stale_sessions_outside_event_window \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_non_strict_every_turn_mode
# 4 passed

python3 tests/integration/agent_planner_protocol_strict_smoke.py
python3 tests/integration/agent_planner_public_surface_smoke.py
# both ok; strict_runtime_scope=true; missing_session_count=0
```

## Scope-contract completion evidence — 2026-06-27

The completion audit now includes a machine-readable scope contract. This closes the ambiguity around whether admin pages, unauthenticated requests, and unknown paths should create Agent Planner sessions.

Live evidence from `/admin/agent-runtime-audit.json?limit=40`:

```text
strict_conversation_scope=supported_authenticated_public_api_paths
conversation_count=6
gateway_owned_count=15
has_auth_exclusion=true
has_404_exclusion=true
has_admin_audit_exclusion=true
```

The implementation is covered by:

```bash
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions
# passed
```

## Final synthesis refusal/scope/non-answer closure — 2026-06-27

The original failing request was found in `request_logs.id=6151`:

- path: `/v1/messages`
- tools: 62 declared client tools
- current user text: `分析这套项目`
- planner state: `workflow=project_analysis`, `step=synthesis`, `evidence_count=4`
- bad old response: `Hello, I can't answer this question for now. Let's talk about something else.`

This proved the failure was not planner entry. The failure was at the final synthesis boundary: chat-only upstream was allowed to override planner evidence with refusal, stale-session path drift, or a non-answer placeholder.

Completion rule now enforced by Gateway:

- If final synthesis upstream refuses, replace with deterministic planner evidence synthesis.
- If final synthesis mentions prior session / correct path / an absolute path outside current planner workspace/evidence, replace with deterministic planner evidence synthesis.
- If final synthesis says it will inspect/check/read later but returns no tool call, replace with deterministic planner evidence synthesis.

Live replay of the old request after restart:

```text
stop=end_turn
workflow=project_analysis
step=synthesis
synthesis_refusal_fallback=False
synthesis_scope_fallback=True
synthesis_nonanswer_fallback=True
has_old_refusal=False
has_wrong_path=False
has_let_me=False
```

The `jo -> 分析这套项目` live path now shows the intended boundary:

```text
jo -> plain_chat / chat_only_synthesis / no tool
分析这套项目 -> project_analysis / project_structure / Bash tool_use
```

Regression and live evidence:

```bash
python3 -m pytest -q tests/test_agent_planner_client_context.py \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_chat_only_refusal_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_cross_session_path_drift_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_final_synthesis_nonanswer_after_evidence
# passed as part of 8-test targeted gate

python3 tests/integration/agent_planner_protocol_strict_smoke.py
python3 tests/integration/agent_planner_public_surface_smoke.py
python3 tests/integration/agent_planner_project_analysis_smoke.py
GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 988 passed, 2 skipped, 21 warnings
```

### Repeatable synthesis guard smoke

A dedicated integration smoke now locks the final-synthesis guard independently of any live upstream randomness:

```bash
python3 tests/integration/agent_planner_synthesis_guard_smoke.py
```

It proves three bad upstream final-synthesis classes are replaced by deterministic planner evidence:

- refusal -> `synthesis_refusal_fallback=true`
- stale session/workspace path drift -> `synthesis_scope_fallback=true`
- non-answer placeholder -> `synthesis_nonanswer_fallback=true`

The smoke also verifies the leaked bad text is absent and the planner evidence marker remains present in the final response.

## Unified acceptance gate — 2026-06-27

A single acceptance command now runs the executable proof set for the Agent Planner runtime:

```bash
./scripts/agent_planner_acceptance.sh
```

The gate covers:

- final synthesis guard;
- full project-analysis planning chain;
- multi-round downstream tool continuation;
- strict Planner envelope across OpenAI/Anthropic chat/messages/responses, streaming and non-streaming;
- all advertised public paths from `/healthz.supported_paths`;
- remote multi-user pressure with scoped sessions, rollups, recall payloads, admin runtime/memory/events/audit;
- long-context pressure with streaming recall, compaction, and cross-tenant leak checks;
- focused history/client-context/final-synthesis regressions;
- direct tool endpoint cloud-boundary regression: user-side workspace tools are rejected by default instead of executing in the Gateway service process.
- authenticated downstream `client_id` conversation requests without workspace metadata, including streaming, use isolated anonymous remote scope instead of Gateway service env/config roots.
- authenticated downstream `client_id` overrides any spoofed body-level `client_id` for internal runtime/planner scope and strips `client_id` before forwarding upstream.
- Gateway-owned public endpoints (`tools/call`, `messages/count_tokens`, assistants/threads/models audit recording) also carry downstream `client_id` into internal runtime scope without polluting upstream/request payloads.
- Gateway-owned HTTP Actions and MCP connectors keep cloud service boundaries: private/localhost HTTP Action URLs, DNS targets, and redirects require explicit admin opt-in, and MCP service-file parameters require explicit admin opt-in instead of treating Gateway service FS as a downstream workspace.
- Gateway-owned WebFetch/WebSearch keep the same cloud network boundary: private/localhost URLs, DNS targets, and redirects require admin-level `allow_private_network_tools`, not caller-supplied arguments.
- Gateway-owned image generation providers keep the same cloud network boundary and clamp requested image dimensions, so downstream prompts cannot silently target service-private provider URLs or huge generation sizes.
- Gateway-owned Memory public tools keep the same cloud tenant boundary: manual memories are scoped by authenticated downstream client + current workspace, spoofed body `client_id` is ignored for scope, and `all_workspaces` is reserved for admin APIs.
- `JsonQuery` is argument-sensitive: `data` remains a pure Gateway helper, but `file_path` / `path` is downstream workspace file access and is surfaced/rejected like other user-side filesystem tools in cloud mode.
- Tool result cache keys include both resolved workspace and request runtime scope, so same tool arguments cannot be reused across authenticated downstream clients/tenants that happen to claim the same workspace string.
- Semantic cache entries include request runtime scope for exact and similarity hits in compatibility mode; persisted semantic cache entries keep the same scope after reload.
- Admin Skill/MCP write endpoints keep service-filesystem boundaries: skill names are single safe catalog segments and browser-origin admin writes are rejected before mutating service-side catalog/config.

Current result:

```text
Agent Planner acceptance gate: PASS
focused pytest: 37 passed
```

Important interpretation: some smoke tests create isolated temporary runtime stores. Their proof is authoritative through the script output, not necessarily through the currently running live service's global admin audit. Use scoped live admin audit for a specific tenant/workspace/session, and the acceptance gate for full cross-feature regression proof.

### Acceptance gate expanded for Gateway-owned compatibility endpoints

The unified acceptance runner now includes dedicated unit/integration checks for Gateway-owned public functionality beyond the minimal public-surface smoke:

```bash
python3 -m pytest -q \
  tests/test_gateway_assistants.py \
  tests/test_gateway_proxy_errors.py \
  tests/test_gateway.py::NativeGatewayTests::test_models_and_count_tokens_endpoints_for_claude_code_compatibility \
  tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions \
  tests/test_gateway.py::NativeGatewayTests::test_direct_user_side_tool_call_requires_downstream_client_by_default \
  tests/test_gateway.py::NativeGatewayTests::test_remote_identity_memory_without_scope_does_not_use_gateway_env_root \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_without_scope_does_not_use_gateway_env_root_for_remote_identity \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_text_tool_fallback_surfaces_declared_user_side_tool
```

This proves assistants/threads, models/count_tokens, upstream error shape, the machine-readable audit scope contract, direct user-side tool rejection for cloud Gateway mode, and streaming text-tool fallback remain compatible while the Agent Planner runtime owns conversation turns.

Current enhanced gate result:

```text
./scripts/agent_planner_acceptance.sh
focused pytest inside gate: 37 passed
Agent Planner acceptance gate: PASS
```

### 2026-07-05 cloud direct-tool boundary check

The direct tool endpoints are Gateway-owned public service surfaces, not a way for the cloud Gateway to touch a downstream user's filesystem/shell/GUI/local agent workspace. Current behavior:

- Gateway-owned service tools such as `calculator` and provider-backed `image_generation` still execute through `/tools/call`, `/v1/tools/call`, and `/v1/functions/call`.
- User-side tools such as `Read`, `Bash`, `computer_use`, `click`, `Agent`, and `Skill` are rejected by default with `failure_type=direct_user_side_tool_requires_downstream_client`, so downstream filesystem/shell/GUI/local-agent work cannot silently run on the Gateway service machine.
- The same rejection now applies when those user-side tools are nested inside `multi_tool_use.parallel`, its `parallel` alias, or top-level `tool_uses`; alias normalization is used only for the ownership check so older tool-call field normalization stays compatible.
- Legacy/local-proxy direct execution remains covered only behind explicit `execute_user_side_tools_in_gateway=true` or `GATEWAY_EXECUTE_USER_SIDE_TOOLS=1`.

Current verification:

```text
python3 tests/integration/agent_planner_public_surface_smoke.py
  ok=true
  direct_tool_result_event_count=5
  direct_tool_error_event_count=2

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 33 passed
  Agent Planner acceptance gate: PASS

python3 -m compileall -q src tests
git diff --check
python3 -m pytest -q
  1017 passed, 2 skipped, 21 warnings in 52.49s
```

### 2026-07-05 Gateway-owned MCP / HTTP Action parameter boundary

Gateway-owned MCP and HTTP Actions are allowed to execute in the cloud Gateway service, but downstream arguments must not silently turn that service into a user-local workspace or private-network pivot.

Current behavior:

- HTTP Action URL validation still allows only absolute `http(s)` URLs, and now rejects localhost/private/non-global IP targets by default, including domain names that resolve to private/non-global IPs and redirects to those targets. A single action may opt in with `allow_private_network: true` for an admin-approved service-private endpoint.
- MCP `tools/call` validates caller-supplied arguments before starting the MCP server. Path/file-like arguments such as `/etc/passwd`, `../...`, `file:///...`, `src/file.py`, or Windows/UNC filesystem paths are rejected by default.
- The generic `mcp_call_tool` builtin follows the same validation because it delegates through `_mcp_call_tool`.
- MCP `resources/read` and `prompts/get` also validate path-like parameters. A server or tool may opt in with `allow_service_file_arguments: true` only when the administrator intentionally exposes service-side filesystem resources.
- `WebFetch`, `WebBrowser`, `WebSearch`, and `web_search_call` reuse the same URL/DNS/redirect validation for Gateway-owned network access. Private-network access for these generic network tools is controlled only by admin config/env (`gateway.allow_private_network_tools` / `GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS`), not by downstream tool arguments.
- `image_generation` is classified as a Gateway-owned provider/network tool, not a downstream desktop GUI tool. It validates OpenAI/Pollinations/Hugging Face provider URLs through the same URL/DNS/redirect policy. Private provider endpoints require the same admin-level opt-in; downstream args cannot grant it. Requested dimensions are clamped by `GATEWAY_IMAGE_MAX_DIMENSION` (default 2048, hard ceiling 4096).

Current verification:

```text
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_http_action_private_network_url_requires_admin_opt_in \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_dns_private_target_requires_admin_opt_in \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_redirect_to_private_network_requires_admin_opt_in \
  tests/test_gateway.py::NativeGatewayTests::test_webfetch_private_network_url_requires_admin_opt_in \
  tests/test_gateway.py::NativeGatewayTests::test_webfetch_dns_private_target_requires_admin_opt_in \
  tests/test_gateway.py::NativeGatewayTests::test_websearch_private_search_url_requires_admin_opt_in \
  tests/test_gateway.py::NativeGatewayTests::test_core_coding_tools_write_edit_shell_and_web_are_real \
  tests/test_gateway.py::NativeGatewayTests::test_image_generation_private_provider_url_requires_admin_opt_in \
  tests/test_gateway.py::NativeGatewayTests::test_image_generation_does_not_fake_success_when_providers_fail \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_exposes_schema_and_executes_real_http \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_get_uses_query_and_expands_env_headers \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_http_error_records_tool_failure \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_response_max_bytes_is_enforced \
  tests/test_gateway.py::NativeGatewayTests::test_mcp_service_file_arguments_require_admin_opt_in \
  tests/test_gateway.py::NativeGatewayTests::test_mcp_stdio_tools_list_call_and_schema_merge \
  tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_mcp_broken_server_marks_health_and_invalidates_cache \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream -q
  18 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 33 passed
  Agent Planner acceptance gate: PASS

git diff --check
python3 -m pytest -q
  1017 passed, 2 skipped, 21 warnings in 52.49s
```

### 2026-07-05 downstream file-path target boundary

Declared downstream client schemas often use `file_path`. In cloud Gateway mode, relative downstream `file_path` values must not be expanded against a Gateway service env/config root when the request did not provide a real client workspace hint.

Current behavior:

- If the request carries explicit client workspace metadata (`workspace`, `workspace_root`, Claude `Worktree`, Codex `<cwd>`, etc.), synthesized downstream `file_path` values can be anchored to that client workspace path.
- If the only available root is Gateway env/config (`GATEWAY_WORKSPACE_ROOT` / configured `workspace_root`), synthesized downstream `file_path` remains relative (for example `README.md`) and does not leak or target the Gateway service filesystem.
- Both gateway-semantic `path` inputs and already schema-shaped `file_path` inputs follow the same boundary rule.
- Relative workspace identifiers such as `workspace="project-a"` are not treated as filesystem paths, are not resolved against the Gateway service current working directory, and do not fall back to Gateway env/config roots. They are kept only as opaque isolation input for the anonymous remote workspace namespace.
- Remote identity metadata (`tenant`/`user_id`/`session_id`/etc.) without any client workspace hint also uses an anonymous remote workspace namespace, not Gateway env/config roots, so identified cloud callers do not share service-root runtime state.
- Conversation memory helpers that run without an active `_workspace_scope()` now derive their memory workspace key only from an active downstream scope or request-provided client workspace/remote identity. They no longer fall back to Gateway env/config service roots for remote identity requests.
- Agent Planner session-key helpers that run without an active `_workspace_scope()` now follow the same rule: remote identity requests are keyed to anonymous remote workspace namespaces instead of Gateway service roots, preventing planner state from being indexed under a cloud server filesystem path.

Regression:

```bash
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_declared_downstream_file_path_does_not_use_gateway_env_without_client_workspace
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_declared_downstream_file_path_anchors_to_explicit_client_workspace
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_relative_workspace_value_is_not_treated_as_gateway_service_path
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_remote_identity_without_workspace_does_not_fall_back_to_gateway_env_root
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_remote_identity_memory_without_scope_does_not_use_gateway_env_root
python3 -m pytest -q tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_without_scope_does_not_use_gateway_env_root_for_remote_identity
```

### 2026-07-05 Gateway-owned Memory tenant boundary

Gateway-owned Memory is service-side state, but public direct tool calls are still downstream-client scoped. In cloud mode, a request body must not be able to turn `Memory` into a cross-tenant or cross-workspace audit endpoint.

Current behavior:

- `SaveMemory` stores manual Memory under the active request tenant scope; authenticated downstream client id from the HTTP layer wins over a spoofed body `client_id`.
- `RecallMemory` / `Memory action=list` filters by exact tenant key and current downstream workspace key.
- public direct tool args `all_workspaces=true` / `include_all_workspaces=true` are rejected with `permission_denied`.
- global Memory review remains available only through admin-authenticated runtime/admin endpoints.

Focused regression:

```text
tests/test_gateway.py::NativeGatewayTests::test_memory_tool_scopes_by_authenticated_client_id_and_blocks_global_listing
```

### 2026-07-05 JsonQuery file-path ownership split

`JsonQuery` has a pure service-side shape and a workspace-file shape:

- `JsonQuery(data=..., query=...)` remains Gateway-owned and may execute in the cloud service.
- `JsonQuery(file_path=.../path=...)` reads a workspace file, so it is downstream-client owned in cloud mode.

The direct public endpoints now reject/surface the file-path shape with the same `direct_user_side_tool_requires_downstream_client` boundary used for `Read`, `Bash`, GUI, local-agent, and `Skill`. The nested `multi_tool_use.parallel` path uses the same argument-sensitive ownership check.

Focused regression:

```text
tests/test_gateway.py::NativeGatewayTests::test_direct_user_side_tool_call_requires_downstream_client_by_default
```

### 2026-07-05 Tool result cache runtime/tenant boundary

Tool result cache is Gateway-owned service state, so cache entries must not be scoped only by a caller-visible workspace string. In cloud mode, multiple authenticated downstream clients can present the same workspace path/name; cacheable service tools still need tenant/runtime separation.

Current behavior:

- Cache key arguments include `__gateway_workspace_cache_key` and `__gateway_runtime_cache_key`.
- The runtime key comes from the active request scope, including authenticated downstream client/tenant/session/workspace ownership.
- The extra sentinel fields are internal cache inputs only and are not passed to the actual tool handler.
- Persistent tool cache storage is covered because the persisted key hashes the full internal arguments object.

Current verification:

```text
python3 -m pytest \
  tests/test_gateway.py::NativeGatewayTests::test_tool_result_cache_keys_include_runtime_scope_not_only_workspace \
  tests/test_gateway.py::NativeGatewayTests::test_more_tool_compat_tree_json_symbols_and_catalog \
  tests/test_gateway.py::NativeGatewayTests::test_direct_user_side_tool_call_requires_downstream_client_by_default -q
  3 passed

python3 -m compileall -q src tests
./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 34 passed
  Agent Planner acceptance gate: PASS
python3 -m pytest -q
  1020 passed, 2 skipped, 21 warnings in 54.10s
git diff --check
  clean
```

### 2026-07-05 Semantic cache runtime/tenant boundary

Semantic cache can bypass the expensive upstream/orchestration path in compatibility mode, so it must obey the same remote-service scope boundary as planner state, memory, and tool result cache.

Current behavior:

- `SemanticCache.get/put(..., scope_key=...)` namespaces exact keys by scope.
- Similarity matching filters candidates to the same `scope_key` before computing cosine similarity.
- `scope_key` is persisted in SQLite and restored on cache reload.
- HTTP non-streaming semantic cache uses authenticated downstream client + resolved workspace/session runtime scope; if scope cannot be computed, the handler does not write an unscoped cache entry.

Current verification:

```text
python3 -m pytest -q \
  tests/test_semantic_cache.py::TestSemanticCache::test_scope_key_isolates_exact_and_semantic_matches \
  tests/test_cache_persistence.py::TestCachePersistence::test_semantic_cache_scope_persists_and_isolates \
  tests/test_persistence.py::TestPersistence::test_semantic_cache_save_and_load
  3 passed

python3 -m compileall -q src tests
./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 36 passed
  Agent Planner acceptance gate: PASS
python3 -m pytest -q
  1021 passed, 2 skipped, 21 warnings in 54.43s
```

### 2026-07-05 Admin Skill/MCP write boundary

Admin install/delete endpoints mutate Gateway-owned service catalog/config, not downstream workspaces. They now enforce both path containment and browser-origin protections.

Current behavior:

- Skill directory names must be a single safe segment; traversal such as `../outside-victim` is rejected.
- `skill-create`, `skill-install`, and `skill-delete` all resolve through the same service-side `skills/` containment helper.
- Skill and MCP install/delete writes call `_check_admin_write()`, so Basic Auth is still required and cross-origin browser POSTs are rejected.

Current verification:

```text
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_admin_skill_delete_rejects_path_traversal_and_cross_origin \
  tests/test_gateway.py::NativeGatewayTests::test_admin_post_rejects_cross_origin_browser_request \
  tests/test_gateway.py::NativeGatewayTests::test_admin_post_allows_same_origin_browser_request
  3 passed

python3 -m compileall -q src tests
./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 37 passed
  Agent Planner acceptance gate: PASS
python3 -m pytest -q
  1021 passed, 2 skipped, 21 warnings in 54.43s
```

### 2026-07-05 Caller-declared builtin name authority

Caller-provided tool schemas are downstream authority. In cloud mode, the
Gateway must not reinterpret a caller-declared private function named
`calculator`, `WebSearch`, or another pure/network builtin as permission to run
the Gateway service's hidden builtin. Only explicit Gateway extension points
(`gateway__*` aliases, HTTP Actions, MCP public names) remain service-owned.

Current behavior:

- `_declared_tool_shadows_gateway_builtin()` detects caller-declared private
  schemas that collide with Gateway builtin names or aliases.
- `_tool_call_requires_downstream_execution()` returns downstream-owned for
  those collisions before builtin execution is considered.
- Chat-only planner preexecution skips shadowed builtins, and the declared
  function planner emits a protocol-level downstream tool call instead.
- Gateway-owned builtin preexecution without a caller-declared colliding schema
  is unchanged.

Current verification:

```text
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_declared_gateway_builtin_name_is_downstream_owned \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_declared_calculator_collision_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_builtin_calculator_preexecutes_without_request_tools \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_web_search_uses_declared_downstream_tool_name \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_custom_function_call_is_surfaced_without_upstream_native_support
  5 passed

python3 -m compileall -q src tests
./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 39 passed
  Agent Planner acceptance gate: PASS
python3 -m pytest -q
  1023 passed, 2 skipped, 21 warnings in 54.65s
git diff --check
  clean
```

### 2026-07-05 Upstream routing-field redaction for cloud workspace hints

The Gateway uses request fields such as `workspace`, `workspace_dir`,
`workspace_root`, and `cwd` only to resolve downstream runtime scope. Those
values can contain private client project paths and must not be forwarded to
the chat-only upstream.

Current behavior:

- `_GATEWAY_INTERNAL_REQUEST_FIELDS` includes `workspace` and `workspace_dir`.
- `_strip_gateway_internal_request_fields()` removes those fields at the
  top level, inside `metadata`, and inside JSON-encoded `metadata.user_id`.
- The same sanitizer is used by normal upstream conversion and streaming
  passthrough, so both paths redact cloud workspace routing hints.

Current verification:

```text
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_internal_workspace_fields_are_not_forwarded_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_streaming_passthrough_strips_internal_workspace_fields
  2 passed

python3 -m compileall -q src tests
./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 41 passed
  Agent Planner acceptance gate: PASS
python3 -m pytest -q
  1023 passed, 2 skipped, 21 warnings in 55.36s
git diff --check
  clean
```

## Completion matrix reference — 2026-06-27

`docs/agent-runtime-completion-matrix.md` is now the authoritative checklist for deciding whether the full Agent Planner objective is complete. It intentionally keeps the goal active until every requirement has current evidence, including full regression and live scoped audit in the same evidence window.


## 2026-06-27 04:46 final evidence window

Current completion matrix status: verified for R1-R25.

Evidence collected in the same final window:

- Full gate: `./scripts/agent_planner_acceptance.sh --full` completed with `Agent Planner acceptance gate: PASS` and full pytest summary `988 passed, 2 skipped, 21 warnings in 51.97s`.
- Live health: `http://127.0.0.1:8885/healthz` returned `ok=true`, `mode=orchestrate`, `fake_prompt_tools=false`, 21 advertised supported paths, and 67 builtin tools.
- Live strict project-analysis dispatch: `/v1/messages` with user text `分析这套项目` returned `stop_reason=tool_use`, tool names `LS`, `Glob`, `Glob`, `strategy=gateway_downstream_tool_request`, `planner_workflow=project_analysis`, `planner_step=project_structure`, `intent_kind=project_analysis`.
- Scoped live audit for tenant `codex-live-user`, workspace `ai_tool_functioncall`, session `codex-live-project-analysis-audit-pass`: `strict_every_turn_planner_envelope=proven/current_scope`, `admin_observability=proven/current_scope`, `covered_session_count=1`, `dispatch_session_count=1`, `missing_session_count=0`.
- Quality gates: `git diff --check` returned clean; tracked secret grep found only documented fake/test keys (`sk-test-*`, `sk-secret-*`, `sk-REDACTED`, `sk-xxx`) and no real `sk-sanbo` in tracked files.

Conclusion: the current implementation satisfies the stated Agent Planner runtime objective under the documented remote-service contract: the Gateway owns planning/tool authority, chat-only upstream is synthesis-only, public surfaces are covered, history and client injected context are bounded, and multi-user/long-context/streaming behavior is tested.
