# Agent Runtime Requirement Audit

本文件记录远端 Agent Planner / Runtime 的机器可读审计方案。重点：Gateway 是远端服务，不是本地增强服务；client workspace 属于调用方；chat-only upstream 只做最终语言综合。

## Endpoint

```text
GET /admin/agent-runtime-audit.json?tenant_contains=&workspace_contains=&session_contains=&limit=200
Authorization: Basic admin:admin
```

可选过滤：

- `tenant_contains`
- `workspace_contains`
- `session_contains`
- `workflow`
- `current_step`
- `event_type`
- `memory_kind` / `kind`
- `limit`，最大 500

## Scope rule

审计函数只消费 handler 已经过滤后的数据：

- Agent Planner sessions
- runtime events
- conversation memories / rollups
- static capability catalog

它不会为了“证明”需求而扩大 tenant/workspace/session 查询范围，因此不会把其他用户证据混进当前用户审计结果。

## Requirement status

- `proven/current_scope`：当前查询 scope 内存在运行时证据。
- `configured/static`：静态能力配置存在，但当前 scope 尚无运行时证据。
- `missing/current_scope`：当前 scope 内缺证据，operator 应继续跑对应 smoke 或查看 dispatch/memory 事件。

## Requirement keys

| Key | 证明内容 | 主要证据 |
| --- | --- | --- |
| `chat_only_upstream_synthesis_only` | 上游只做 synthesis，无工具权限 | `chat_only_synthesis_boundary`, `upstream_tool_attempt_ignored`, `tool_authority_granted=false` |
| `planner_owns_intent_and_workflows` | Planner 管 intent、workflow、状态机与 dispatch | `intent_classification`, `planner_state`, `tool_dispatch`, workflow/intents catalog |
| `downstream_client_workspace_tools` | 文件、shell、GUI、本地 agent、caller-private tools 走客户端 workspace | `tool_dispatch` + `owner=downstream_client` |
| `gateway_owned_service_tools` | 纯服务/网络/connector 工具可由 Gateway 执行 | `gateway_tool_execute`, `gateway_tool_result` |
| `infinite_context_memory_rollup` | 长上下文通过 scoped memory rollup/recall 支撑 | `memory_rollup`, `session_rollup` memory |
| `tenant_workspace_isolation` | 多用户并发按 tenant/workspace/session 隔离 | scoped filters + filtered events/memories/sessions |
| `streaming_nonstreaming_parity` | streaming 与非 streaming 共享 Planner 边界 | `chat_only_synthesis_boundary` with `source=streaming` and `source=non_streaming` |
| `admin_observability` | operator 可查询能力、planner、memory、events 和 audit | admin endpoint catalog |

## Regression test

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements -q
```

该测试会同时写入当前 tenant 与其他 tenant 的 runtime/memory marker，并验证 audit payload 不包含其他 tenant marker，也不包含 Gateway 服务 workspace 路径。

## Pressure smoke coverage

`tests/integration/agent_planner_remote_pressure_smoke.py` 已把 audit endpoint 纳入真实远端压力路径：

1. 6 个 tenant / 6 个 client workspace 并发请求 downstream `Read`。
2. 每个 tenant 写入 conversation memory 并触发 `session_rollup`。
3. admin scope 额外触发 Gateway-owned `calculator`，产生 service-side tool evidence 与 chat-only synthesis boundary。
4. HTTP 查询 `/admin/agent-runtime-audit.json`。
5. 验证当前 scope 的核心 requirement 为 `proven/current_scope`，并确认其他 tenant marker 未泄漏。

```bash
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; admin_audit_checked=true
```

## Streaming parity is now required in remote pressure smoke

远端压力 smoke 已收紧：`streaming_nonstreaming_parity` 不再允许缺失。测试在同一个 admin tenant/workspace/session scope 内分别触发：

- non-streaming Gateway-owned `calculator` final synthesis；
- streaming Gateway-owned `calculator` final synthesis。

二者都必须记录 `chat_only_synthesis_boundary`，且 metadata source 分别为 `non_streaming` 与 `streaming`。随后 `/admin/agent-runtime-audit.json` 必须返回：

```json
{
  "requirements": {
    "streaming_nonstreaming_parity": {
      "status": "proven/current_scope"
    }
  }
}
```

关键测试约束：streaming 路径必须通过正式 `run_streaming_orchestration()`，不能直接调用 `_run_streaming_orchestration_scoped()`，否则会绕过 workspace resolution，导致 runtime event 落入 `workspace:unavailable`。

## Legacy gateway mode detection

Completion audit 现在显式检查当前运行模式：

```json
{
  "runtime_config": {
    "gateway_tool_mode": "orchestrate",
    "upstream_tools_enabled": "adapter",
    "legacy_gateway_passthrough": false
  },
  "requirements": {
    "agent_planner_runtime_mode": {
      "status": "proven/current_scope"
    }
  }
}
```

如果 `gateway.tool_mode` 是 `passthrough`、`native_passthrough` 或 `proxy`，则：

```json
{
  "requirements": {
    "agent_planner_runtime_mode": {
      "status": "missing/current_scope",
      "detail": {
        "legacy_gateway_passthrough": true
      }
    }
  },
  "overall_status": "needs_runtime_evidence"
}
```

这防止旧 gateway 兼容模式被误判为完整 Agent Planner Runtime。

## Upstream native tool authority detection

Agent Runtime audit 现在不仅检查 `gateway.tool_mode`，也检查上游是否被配置成 native-tool capable。目标状态是：上游只负责最终对话综合，不持有工具权限。

正常状态：

```json
{
  "runtime_config": {
    "upstream_tools_enabled": "adapter",
    "upstream_supports_tools": false,
    "upstream_supports_function_calls": false,
    "upstream_native_tool_authority": false
  },
  "requirements": {
    "chat_only_upstream_config": {
      "status": "proven/current_scope"
    }
  }
}
```

如果 `upstream.tools_enabled=auto/native` 且 `supports_tools=true`、`supports_function_calls=true`，audit 会标红：

```json
{
  "requirements": {
    "chat_only_upstream_config": {
      "status": "missing/current_scope",
      "detail": {
        "upstream_native_tool_authority": true
      }
    }
  },
  "overall_status": "needs_runtime_evidence"
}
```

这防止通过配置重新把工具权限交给 chat-only upstream。

## User-machine tool execution policy detection

Agent Runtime audit now checks whether Gateway is configured to execute user-machine tools locally. The target remote-service state is:

```json
{
  "runtime_config": {
    "gateway_execute_user_side_tools": false,
    "gateway_delegate_tools_to_downstream": null,
    "gateway_forces_local_user_side_tools": false
  },
  "requirements": {
    "downstream_client_tool_execution_policy": {
      "status": "proven/current_scope"
    }
  }
}
```

If `execute_user_side_tools_in_gateway=true`, audit reports:

```json
{
  "requirements": {
    "downstream_client_tool_execution_policy": {
      "status": "missing/current_scope",
      "detail": {
        "gateway_forces_local_user_side_tools": true
      }
    }
  },
  "overall_status": "needs_runtime_evidence"
}
```

This keeps filesystem, shell, GUI, local-agent, and caller-private tools anchored to the downstream client workspace instead of the Gateway service process.

`delegate_tools_to_downstream=false` no longer grants the cloud Gateway authority to run user-machine tools locally; it only affects otherwise Gateway-executable service/tool calls.


## Audit evidence-window semantics — 2026-06-27

`/admin/agent-runtime-audit.json` now separates **response limit** from **audit evidence window**:

- `limit` remains the operator-facing requested limit and is capped at 500.
- `audit_limit` defaults to 500 and is also capped at 500.
- The audit still applies the same tenant/workspace/session/workflow/event filters before constructing requirements; it does not widen scope across users.

Strict every-turn proof uses only sessions represented by an `intent_classification` event in the current filtered evidence window. Durable planner sessions outside the current evidence window, or historical sessions created before strict mode, are not treated as current missing evidence. A session with `intent_classification` but no planner-owned boundary (`chat_only_synthesis_boundary`, `tool_dispatch`, `gateway_tool_execute/result`) is still reported as missing.

Unscoped global audits cannot prove tenant/workspace/session isolation at runtime because there is no tenant/workspace/session filter. In that case `tenant_workspace_isolation` may be `configured/static`; runtime proof requires scoped filters or `tests/integration/agent_planner_remote_pressure_smoke.py`.

Regression test:

```bash
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_ignores_stale_sessions_outside_event_window
```

## Current live transcript requirement proof — 2026-06-27

The live `jo -> 分析这套项目` transcript now proves these requirements for the current process:

- **Every conversational turn enters Agent Planner**: `jo` creates a plain-chat planner envelope; `分析这套项目` creates a project-analysis planner envelope.
- **Planner owns intent, not upstream**: `jo` remains plain chat even with declared tools; project analysis becomes planner-owned `Bash` dispatch.
- **Chat-only upstream has no tool authority**: project-analysis tool dispatch is generated by Gateway Agent Planner; final synthesis strips tool surfaces before upstream.
- **Client workspace remains caller-owned**: user-machine `Bash` is delegated downstream and uses request workspace metadata (`/Users/sanbo/Desktop/ti` or caller workspace), not a service-side project root.
- **Multi-user/long-context surfaces remain covered**: latest pressure and long-context smokes prove scoped memory rollup/recall and cross-tenant leak checks.

Passing evidence collected on current service:

```bash
python3 -m pytest -q tests/test_agent_planner_client_context.py \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_client_injected_user_reminders_for_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_plain_chat_is_wrapped_by_agent_planner_envelope
# 3 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
python3 tests/integration/agent_planner_public_surface_smoke.py
python3 tests/integration/agent_planner_protocol_strict_smoke.py
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# all ok
```

## Global vs scoped strict every-turn audit — 2026-06-27

Strict every-turn runtime proof now requires a scoped audit filter. This is intentional for a remote multi-user service:

- Unscoped global views mix tenants, anonymous workspaces, bounded event windows, aborted requests, and historical pre-instrumentation sessions.
- Therefore global `/admin/agent-runtime-audit.json` is an operator overview, not a proof that every current tenant/session is strict.
- Runtime proof must use at least one of `tenant_contains`, `workspace_contains`, or `session_contains`.
- Scoped proof is still strict: a scoped session with `intent_classification` but without a planner-owned boundary remains `missing/current_scope`.

Global detail fields:

```json
{
  "strict_every_turn_planner_envelope": {
    "status": "configured/static",
    "detail": {
      "runtime_scope_required": true,
      "strict_runtime_scope": false,
      "unscoped_intent_session_count": 59,
      "missing_session_count": 0
    }
  }
}
```

Current live proof after the change:

```text
GET /admin/agent-runtime-audit.json?limit=120
  missing=0
  strict status=configured/static

python3 tests/integration/agent_planner_protocol_strict_smoke.py
  covered_session_count=12
  missing_session_count=0
  strict_runtime_scope=true
```

## Agent Runtime scope contract — 2026-06-27

`/admin/agent-runtime-audit.json` now returns a machine-readable `scope_contract` so the strict Planner claim has an explicit boundary.

Contract:

```json
{
  "strict_conversation_scope": "supported_authenticated_public_api_paths",
  "conversation_paths": ["/v1/chat/completions", "/v1/messages", "/v1/responses", "...anthropic aliases..."],
  "gateway_owned_service_paths": ["/v1/models", "/v1/tools/call", "/v1/functions/call", "..."],
  "control_plane_paths_excluded": ["/healthz", "/ui", "/admin/agent-runtime-audit.json", "..."],
  "security_layer_excluded": {
    "auth_failures": "rejected before request body/session metadata is trusted",
    "admin_auth_failures": "admin control plane uses Basic auth and does not create planner sessions",
    "unsupported_paths": "404 before planner because no protocol/workspace/session contract exists"
  }
}
```

Meaning:

- Every authenticated supported conversation path must enter Agent Planner.
- Every Gateway-owned public endpoint must produce Gateway runtime evidence.
- Direct tool endpoints (`/tools/call`, `/v1/tools/call`, `/v1/functions/call`) execute Gateway-owned service tools only in cloud mode. User-side workspace tools such as `Read`, `Bash`, `Skill`, GUI, or local agent tools must run in the downstream client workspace and are rejected unless explicit local-proxy execution is enabled.
- Declared downstream `file_path` arguments are anchored only when the request supplies an absolute downstream client workspace hint; Gateway env/config roots and relative workspace identifiers are not converted into downstream absolute file targets. This applies both to gateway-semantic `path` inputs and already schema-shaped `file_path` inputs. Relative logical workspace ids remain opaque namespace inputs and do not fall back to the Gateway service workspace root. Remote tenant/session identity without a workspace hint also uses anonymous remote workspace isolation instead of the Gateway env/config root.
- Admin/control-plane, auth failures, and unsupported paths are pre-conversation/security surfaces, not user-agent conversations.
- Runtime proof still requires scoped tenant/workspace/session audit.

Regression:

```bash
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_direct_user_side_tool_call_requires_downstream_client_by_default
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_declared_downstream_file_path_does_not_use_gateway_env_without_client_workspace
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_declared_downstream_file_path_anchors_to_explicit_client_workspace
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_relative_workspace_value_is_not_treated_as_gateway_service_path
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_remote_identity_without_workspace_does_not_fall_back_to_gateway_env_root
```

## Requirement update: chat-only upstream cannot own final Agent answer — 2026-06-27

Requirement clarified by live failure: every supported conversation request must enter Agent Planner, but that is not sufficient. For planner-owned workflows, final text must also remain bounded by Gateway-owned planner evidence.

Current invariant:

1. Gateway classifies intent and owns workflow/state/evidence.
2. Downstream client tools execute in the caller workspace.
3. Chat-only upstream may only synthesize wording.
4. If upstream synthesis refuses, drifts into another session/workspace, or emits a placeholder such as “Let me first see...” without a tool call, Gateway replaces it with deterministic planner evidence synthesis.

Covered failure classes:

- old refusal: `Hello, I can't answer this question for now. Let's talk about something else.`
- stale context: `上一个 session` / `正确的路径` / path outside current planner workspace evidence
- non-answer placeholder: `Let me first see/check/inspect...` / `我先看看`

Proof from current live service:

```text
old request_logs.id=6151 replay:
  workflow=project_analysis
  step=synthesis
  old refusal leaked=false
  wrong path leaked=false
  let-me placeholder leaked=false

jo -> 分析这套项目:
  jo intent=plain_chat
  分析这套项目 intent=project_analysis
  stop=tool_use
  tool=Bash
```

Acceptance gates passed:

```text
Targeted planner/client-context regressions: 8 passed
Protocol strict smoke: ok=true, covered_session_count=12, missing_session_count=0
Public surface smoke: ok=true, advertised_count=21
Project analysis smoke: ok=true
Full pytest: 988 passed, 2 skipped, 21 warnings
```

### Added acceptance gate: synthesis guard smoke

Requirement: chat-only upstream must not be able to produce the final Agent answer if it contradicts, ignores, or defers planner evidence.

Acceptance gate:

```bash
python3 tests/integration/agent_planner_synthesis_guard_smoke.py
```

Pass criteria:

- all cases return `ok=true`;
- refusal/scope/nonanswer bad upstream text is not leaked;
- planner evidence marker is included in the fallback;
- corresponding `gateway_agent_planner.synthesis_*_fallback` metadata flag is true.

## Requirement audit execution contract — 2026-06-27

The full remote Agent Planner requirement set cannot be proven by one unscoped live admin query, because several requirements are intentionally scenario-specific:

- strict conversation envelope: proven by scoped conversation sessions;
- public surface: proven by calling every advertised path;
- long-context memory: proven under pressure/compaction scenarios;
- multi-user isolation: proven with concurrent tenants/workspaces;
- final synthesis quality: proven with controlled weak-upstream bad outputs.

`./scripts/agent_planner_acceptance.sh` is now the unified executable audit contract. Passing it is the minimum smoke-level proof that the current tree still supports the requested Agent Planner mode across protocols, public endpoints, history handling, final synthesis, multi-user scope, memory rollups, and streaming/non-streaming parity.

### Public functionality coverage added to acceptance gate

To move closer to “每一个功能都必须支持”, the acceptance contract now includes dedicated tests for Gateway-owned public endpoints and compatibility behavior:

- assistants and threads creation;
- workspace metadata override rules for Gateway-owned endpoints;
- upstream proxy error preservation;
- models and count_tokens compatibility;
- scope contract boundary for conversation vs Gateway-owned vs control-plane/security-layer paths;
- direct user-side tool rejection on Gateway-owned direct tool endpoints by default, preserving the downstream client workspace boundary;
- declared downstream `file_path` target handling, ensuring Gateway service roots and relative logical workspace ids are not surfaced as user workspace paths unless an absolute client workspace hint is present, already schema-shaped `file_path` args follow the same rule as gateway-native `path` args, and relative ids / identity-only remote requests are isolated without resolving against service cwd/env/config roots.

These complement `agent_planner_public_surface_smoke.py`, which executes every path advertised by `/healthz.supported_paths`.

## Completion matrix reference — 2026-06-27

See `docs/agent-runtime-completion-matrix.md` for the current requirement-by-requirement completion matrix. It maps the user objective to 25 concrete requirements and defines the evidence required before a final completion claim is allowed.

Important rule: `./scripts/agent_planner_acceptance.sh` is smoke-level proof; `./scripts/agent_planner_acceptance.sh --full` plus live scoped audit and static checks are required before marking the overarching goal complete.
