# Remote Agent Planner / Agent Runtime Architecture

更新时间：2026-06-26

## 定位

本项目当前目标不是给本机 CLI 做一个“加强服务”，而是在远端 Gateway 中提供一个外层 Agent Planner / Agent Runtime。上游模型可以是 chat-only；上游只负责最终对话与综合，不被授予本地工具权限。

## 核心边界

1. **上游 chat-only**
   - 请求进入最终综合前剥离 `tools` / `tool_choice`。
   - 上游返回的伪工具调用只记录为事件，不直接获得工具执行权。

2. **Agent Planner 拥有工具编排权**
   - 负责意图识别、workflow registry、状态机、下一步工具决策、证据归档、最终综合上下文注入。
   - workflow 以 transition table 驱动，覆盖 project analysis、code search、test/build、fix loop、QA loop、generic tool、edit、gateway-owned tool、chat-only synthesis。

3. **工具所有权分层**
   - Gateway-owned：calculator、current_time、WebSearch、HTTP Actions、配置型 MCP/connectors，可在远端 Gateway 内执行。
   - Downstream/client-owned：Read、Write、Edit、Bash、Skill、GUI/本机 agent 工具、调用方私有 function，默认只向客户端返回工具请求，由客户端 workspace 执行。

3.1 **Chat-only boundary 可观测性**
   - 进入最终综合时，Runtime 记录 `chat_only_synthesis_boundary` 事件。
   - 事件字段证明 `tools/tool_choice` 已被剥离，且 `tool_authority_granted=false`。
   - streaming 和非 streaming 都覆盖该事件。

4. **client workspace 隔离**
   - 请求 workspace 来源优先级：`workspace_root`/`gateway_workspace`、客户端 session metadata、显式测试 env/config、匿名隔离空间。
   - 不允许 fallback 到 Gateway 服务 checkout/cwd。
   - runtime scope 同时包含 tenant/session/workspace；caller-visible id 不再作为全局唯一键使用。

5. **多用户并发隔离**
   - Planner SQLite store 使用 WAL、busy timeout、锁保护 lazy singleton 和 session/event 写入。
   - planner session、runtime events、conversation memories 均索引 tenant/workspace/session。
   - admin JSON API 支持 tenant/workspace/session/event 过滤，避免跨用户可观测数据混杂。

6. **无限上下文/记忆**
   - 对话按 tenant/workspace/session 写入 SQLite memory。
   - 超限前压缩；周期 rollup；召回时重新注入。
   - `/v1/responses` 必须注入 `input`，streaming 和非 streaming 路径保持一致。

## 当前关键实现点

- `src/gateway_agent_planner.py`
  - `WORKFLOW_REGISTRY` / `INTENT_REGISTRY`
  - `plan_downstream_tool_request()`
  - `prepare_upstream_body()`
  - `AgentPlannerStore`
  - `record_runtime_event()` / `list_runtime_events()`
- `src/gateway_tool_runtime.py`
  - `_request_workspace_root()`：不使用服务 cwd。
  - `_workspace_scope(root, body)`：设置 workspace + tenant/session/workspace runtime scope。
  - `_should_use_chat_only_synthesis_boundary()`：决定是否进入 chat-only final synthesis。
- `src/gateway_streaming.py`
  - streaming 入口现在与非流式一致：`_workspace_scope(_request_workspace_root(body), body)`。
- `src/gateway_context.py`
  - memory rollup / recall / Responses `input` 注入。
- `src/gateway_admin.py` / `src/gateway_http_handler.py`
  - admin runtime/memory/events scoped 查询。

## 长上下文压力补充（2026-06-26）

新增 `tests/integration/agent_planner_long_context_pressure_smoke.py`，专门证明：远端 4 tenant / 4 client workspace 并发写入大上下文后，周期 rollup 可被 streaming `/v1/responses` 召回；即使上游上下文窗口设置很小，进入上游的 payload 也会先被 compact，且不泄漏其他 tenant marker。

该轮修复了两个核心稳定性问题：

- Responses `input` list 中 recent `{role, content: string}` 也必须裁剪。
- Planner intent classification 必须对巨型当前输入有界化，不能在工具规划前扫描无限长文本。

```bash
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; rollups_checked=4; streaming_responses_recall_checked=4; compaction_checked=true; cross_tenant_leak_checked=true
```

## 验收证据（2026-06-26 最新）

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_entry_sets_remote_runtime_scope_from_request_body \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# 4 passed

python3 -m pytest -q
# 959 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true

git diff --check
# pass

# bearer token literal audit
# no output outside ignored local runtime/config files
```

## 仍需继续关注

- 长上下文更高压力：当前 pressure smoke 覆盖 6 users 和 rollup/recall/admin，但还可以增加更长 turn、更大 payload 的 soak 测试。
- Admin UI 展示层：JSON API 已有 scope 过滤，后续应确认页面交互默认不会误显示其他 tenant 数据。
- 部署形态：生产需要把 workspace metadata 作为客户端协议契约，而不是依赖 env/config 测试兜底。

## 需求审计面（2026-06-26）

新增 `/admin/agent-runtime-audit.json`，用于把远端 Agent Runtime 的核心要求映射到机器可读证据。该接口不是新的执行路径，只是 operator observability：它复用 scoped planner sessions、runtime events、conversation memories 和 static capability catalog，输出每个 requirement 的 `proven/current_scope`、`configured/static` 或 `missing/current_scope`。

关键原则：审计只使用当前 query scope 已过滤的数据，不为了证明需求扩大查询范围；因此可以用于多租户远端服务排障，不会把其他用户 workspace/memory/event 混入当前用户结论。

已覆盖 requirement keys：

- `chat_only_upstream_synthesis_only`
- `planner_owns_intent_and_workflows`
- `downstream_client_workspace_tools`
- `gateway_owned_service_tools`
- `infinite_context_memory_rollup`
- `tenant_workspace_isolation`
- `streaming_nonstreaming_parity`
- `admin_observability`

测试：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements -q
# 1 passed
```

## Chat-only synthesis refusal guardrail

Final synthesis is not allowed to leak a generic upstream refusal when the outer Agent Planner already owns workflow state and evidence. The upstream model is only a language synthesizer; if it replies with generic refusal text such as “I can't answer this question” during `agent_planner_final_synthesis`, the Gateway replaces that text with a deterministic synthesis from planner state/evidence.

This guardrail is intentionally narrow:

- active only when `gateway_context.strategy=agent_planner_final_synthesis` or `gateway_context.agent_planner` is present;
- preserves successful upstream synthesis unchanged;
- covers Anthropic Messages, OpenAI Chat Completions, and OpenAI Responses shapes;
- runs in both streaming and non-streaming orchestration paths.

The guardrail prevents the old failure mode where tool scheduling succeeded but the final chat-only model ignored evidence and told the user to change topic.

## Strict every-turn planner envelope

`gateway.agent_planner_strict_every_turn=true` is the remote Agent Runtime contract for chat-only upstreams. In this mode, even a normal greeting is not sent as an unclassified raw chat request. The Gateway first classifies it, persists planner state, injects a planner envelope, strips tool surfaces, and only then calls the chat-only upstream for text synthesis.

This separates two concepts:

1. **Planner envelope**: Gateway-owned intent/state/evidence attached to the request/response.
2. **Chat-only synthesis boundary**: tool surfaces are stripped and upstream tool attempts are ignored.

Strict remote deployments enable both for every turn. Compatibility deployments may keep strict mode disabled so legacy native/text tool loops continue to execute while planner-owned evidence turns still use the boundary.

## Client-injected context is not planner intent

Remote Agent Planner classification must distinguish three channels:

1. **Human current instruction**: allowed to drive intent, workflow, and downstream tool dispatch.
2. **Tool evidence / memory evidence**: allowed to inform state and final synthesis, but not to create a new current intent by itself.
3. **Client/runtime injected context**: `<system-reminder>`, `SessionStart`, `PreToolUse/PostToolUse`, `<context_guidance>`, global CLAUDE/AGENTS instructions, recalled summaries, and other harness text. This context must never be treated as the user's current instruction.

The planner now sanitizes classification input before any workflow decision:

- `_planner_user_text()` scans structured messages from newest to oldest and returns only non-tool, non-injected user text.
- `_planner_conversation_text()` aggregates filtered visible user/assistant text and skips system-role hook text.
- When structured messages exist but all visible text is injected runtime context or tool evidence, the conversation text is empty; the planner must not fall back to raw JSON because raw JSON can contain hook strings such as “run tests”.
- `prepare_upstream_body()` uses the sanitized planner conversation, not raw `_conversation_text()`, for strict every-turn intent classification.

This prevents the failure mode where a one-word user message such as `jo` is misclassified as `test_build` because the client included a global instruction saying to run tests after code changes.

## Strict audit boundary semantics

`strict_every_turn_planner_envelope` does not mean every session must already be at final synthesis. A project/code/test workflow may legitimately be in an intermediate downstream tool-dispatch turn. The strict audit therefore treats a session as covered when it has:

1. an `intent_classification` event; and
2. at least one planner-owned boundary event:
   - `chat_only_synthesis_boundary` for final chat-only synthesis;
   - `tool_dispatch` for downstream client workspace tool requests;
   - `gateway_tool_execute` / `gateway_tool_result` for Gateway-owned service tools.

A scoped session missing both synthesis and dispatch boundaries is still a real failure, because it means the request did not pass through a verifiable Agent Planner boundary.

## Integration smoke contract for strict remote mode

Remote-mode integration tests must enable `gateway.agent_planner_strict_every_turn=true`; otherwise they only prove compatibility mode. The pressure smoke now treats `strict_every_turn_planner_envelope` as a required audit key and configures strict mode in its temporary gateway config, so it verifies the same contract expected from the live remote service:

- every scoped session has Agent Planner intent classification;
- each session reaches a planner-owned boundary (`tool_dispatch`, Gateway-owned tool event, or final `chat_only_synthesis_boundary`);
- downstream workspace tools stay client-owned;
- Gateway-owned service tools are preexecuted service-side;
- streaming and non-streaming final synthesis share planner boundaries;
- tenant/workspace/session filters prevent cross-user memory leakage.

## Strict protocol smoke

`tests/integration/agent_planner_protocol_strict_smoke.py` is the protocol-level acceptance check for the strict remote contract. It proves that the public conversation paths are not accidentally bypassing the outer planner:

- `/v1/chat/completions`
- `/v1/responses`
- `/v1/messages`
- streaming and non-streaming variants

For each path, the Gateway must inject planner context, enter the chat-only synthesis boundary, strip tool surfaces before forwarding to the upstream model, and record runtime evidence that admin audit can prove. The smoke uses a fake upstream so it can run in CI/offline without the real Mimo service.

## Strict Agent Planner vs semantic cache

In `gateway.agent_planner_strict_every_turn=true` mode, HTTP-level semantic cache must not return a response before `run_tool_orchestration()` / `run_streaming_orchestration()` has owned the turn. A cache hit at that layer skips intent classification, tenant/session/workspace-scoped runtime events, and the required planner-owned boundary (`chat_only_synthesis_boundary`, `tool_dispatch`, or Gateway-owned tool execution). It can also return stale `gateway_context` from another path or session.

Current rule: strict every-turn mode disables this HTTP semantic-cache bypass. Legacy/compatibility mode may still cache plain non-tool assistant answers.

Regression evidence: `tests/integration/agent_planner_protocol_strict_smoke.py` covers `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, and `/anthropic/v1/*` aliases in both streaming and non-streaming modes and requires 12/12 sessions to be covered by the strict planner envelope.

## Gateway-owned management endpoints for weak upstreams

The remote Gateway must not advertise a public path that can only succeed when the chat-only upstream happens to implement that unrelated API family. `/v1/assistants` and `/v1/threads` are therefore Gateway-owned exact base endpoints:

- `POST /v1/assistants` returns an Assistant-compatible object locally.
- `POST /v1/threads` returns a Thread-compatible object locally.
- These requests do not enter chat synthesis and are not forwarded to the upstream model.

This keeps the public `healthz.supported_paths` truthful while preserving the core split: Gateway owns API/runtime/workflow/state, weak upstream owns only text synthesis.

## Remote client workspace metadata priority

A remote multi-user Gateway must treat the caller's workspace metadata as the source of truth. The service process workspace and service-side `GATEWAY_WORKSPACE_ROOT` are not allowed to override explicit client workspace fields.

Current accepted client workspace fields include:

- top-level `workspace_root`, `gateway_workspace`, `workspace`;
- `metadata.gateway_workspace`, `metadata.workspace_root`, `metadata.project_dir`, `metadata.projectDir`, `metadata.workspace`, `metadata.workspace_dir`, `metadata.cwd`, `metadata.working_directory`, `metadata.primary_working_directory`, `metadata.worktree`;
- detected Claude/Codex textual environment markers such as `Worktree:`, `Primary working directory:`, and `<cwd>...</cwd>`.

Regression proof: live `jo` request with `metadata.workspace=/Users/sanbo/Desktop/ti` produced a planner session key scoped to `/Users/sanbo/Desktop/ti` and log line `Workspace resolved via [session_metadata]: /Users/sanbo/Desktop/ti`, despite the service env fallback pointing at the gateway repo.

## Health advertised paths are executable contracts

`/healthz.supported_paths` is not a marketing list. Every advertised path must be callable by clients and must not depend on accidental upstream support for unrelated API families.

The regression gate is `tests/integration/agent_planner_public_surface_smoke.py`:

1. start a fake chat-only upstream;
2. start a strict Agent Planner Gateway;
3. fetch `/healthz.supported_paths`;
4. call every advertised path, including `/anthropic/v1/*` aliases;
5. fail on any 5xx or unexpected payload shape;
6. verify strict Agent Planner audit covers all conversation-path sessions.

This complements protocol-specific smokes: public-surface smoke proves advertised reachability, while strict protocol/remote-pressure smokes prove deeper planner state, streaming parity, memory isolation, and multi-user behavior.

### Direct tool/function endpoints and workspace ownership

Direct tool/function endpoints are not chat conversations, but they still must obey the remote workspace ownership contract when executing workspace-scoped tools. The public surface smoke therefore treats them as executable workspace-bound operations instead of trivial calculator calls.

The smoke places an identically named file in both service and client workspaces and calls `Read` through every direct tool/function public path. Passing requires the response to read the client workspace file and not the service workspace file. This prevents regressions where service-side environment fallback silently becomes the execution root for a remote user's direct tool request.

## Direct endpoints are runtime-audited boundaries

Direct tool/function endpoints (`/tools/call`, `/v1/tools/call`, `/v1/functions/call` and aliases) are not natural-language conversations, so they do not need chat-only synthesis. They are still remote Gateway communications and must leave an Agent Runtime audit trail.

Execution now records two scoped runtime events inside the resolved client workspace scope:

- `direct_tool_execute`
- `direct_tool_result`

These events use the same tenant/session/workspace key derivation as planner runtime events. The requirement audit treats them as Gateway-owned service-tool evidence, so operators can prove direct endpoint execution without inspecting request logs manually.

### Direct endpoint invalid-input boundary

Direct endpoint request parsing is part of the runtime boundary. Invalid input such as a missing tool/function name is handled as client error, not gateway crash:

- HTTP status: `400`
- error detail: `failure_type=invalid_input`
- runtime event: `direct_tool_error`

The event is recorded after resolving the client workspace, so failed direct requests remain tenant/session/workspace scoped and visible to admin audit tools.

## Tool result cache is workspace scoped

Cacheable read-only tool results are not globally reusable across remote users. The same tool arguments can mean different files in different client workspaces. Tool result cache keys therefore include the resolved workspace key via an internal cache-only sentinel, preventing `Read("marker.txt")` in one workspace from satisfying another workspace's request.

## Tenant metadata contract

Remote clients may send tenant identity as `metadata.tenant`; this is now a first-class alias alongside `metadata.tenant_id`, `metadata.account_id`, `metadata.organization_id`, nested `metadata.user_id`, `metadata.user`, top-level `user`, and `client_id`.

The same tenant normalization path must be used by:

- Planner session keys;
- Runtime event tenant keys;
- memory session keys;
- anonymous isolated workspace derivation for requests without explicit client workspace;
- admin audit filters.

This matters because `metadata.workspace` alone is not enough for a remote multi-user Agent Planner service. Workspace identifies where user-machine tools should execute; tenant identifies who owns the planner/runtime/memory state. A request with `metadata.tenant=alice` and `metadata.workspace=/repo` must be auditable as tenant `alice`, not `anonymous`.

Current live proof shape:

```text
/v1/messages metadata.tenant=live-tenant-alias-user metadata.workspace=/Users/sanbo/Desktop/ti user=jo
session_key=/v1/messages:/Users/sanbo/Desktop/ti:tenant:live-tenant-alias-user:session_id:tenant-alias-jo
runtime event tenant_key=live-tenant-alias-user
strict audit missing_session_count=0
```

Strict Agent Planner mode is also the default in generated config/templates. Compatibility mode should be an explicit opt-out, not the default for a remote chat-only-upstream tool adapter.

## count_tokens as a Gateway-owned runtime boundary

`count_tokens` endpoints are Gateway-owned utility endpoints. They do not need upstream model synthesis and they do not dispatch downstream client tools, but they are still public remote communications and therefore must be observable in Agent Runtime.

The handler now records:

- `token_count_execute`
- `token_count_result`

inside the resolved request workspace scope. Metadata includes `source=token_count_endpoint`, `owner=gateway_service`, `success=true`, and the computed `input_tokens` value.

This prevents a blind spot where `/healthz.supported_paths` advertised count-token endpoints but the runtime audit could not prove their tenant/workspace/session boundary. Public surface smoke requires all four canonical/Anthropic count-token paths to emit `token_count_result` events scoped to the client workspace.

## Gateway-owned public API runtime boundaries

Not every public endpoint is a chat turn. Some endpoints are Gateway-owned management/compatibility utilities, for example models, assistants, threads, count_tokens, and direct tool/function calls. They still count as remote public communications and therefore must be runtime-auditable.

Current Gateway-owned public endpoint events include:

- `models_result` / `models_error`
- `assistants_result`
- `threads_result`
- `token_count_execute` / `token_count_result`
- `direct_tool_execute` / `direct_tool_result` / `direct_tool_error`

These events use the same tenant/workspace/session derivation as planner events. For GET `/v1/models`, optional query metadata (`tenant`, `workspace`, `session_id`) can scope the runtime event without changing the model-list response payload. If no tenant/workspace metadata is supplied, the endpoint still returns normally, but operators should prefer scoped metadata in remote multi-user deployments when they need per-tenant audit evidence.

The public surface smoke treats `/healthz.supported_paths` as an executable contract: every advertised path must be callable and, where it is not a chat synthesis path, must leave Gateway-owned Runtime evidence.

### Gateway-owned public API error boundaries

Gateway-owned public APIs must emit Runtime events on both success and failure. In particular, `/v1/models` may depend on an upstream models endpoint, so upstream failures are recorded as `models_error` while the HTTP response remains a client-visible 502. The event stays tenant/workspace/session scoped via the query/body metadata used for the request.

Public smoke now verifies the failure shape by forcing the fake upstream models endpoint to return 503 and requiring `models_error` evidence.

## Final synthesis quality gate

Agent Runtime now treats final synthesis as a bounded Gateway-controlled boundary, not as arbitrary upstream chat.

Architecture rule:

- The Gateway Agent Planner owns intent, workflow state, evidence, session isolation, and audit.
- The upstream model used behind a weak/chat-only provider is only a wording synthesizer.
- Therefore upstream text is accepted only when it is consistent with planner evidence and current scope.

`src/gateway_agent_planner.py::apply_synthesis_refusal_fallback()` now acts as a final synthesis quality gate. It inspects upstream text after planner evidence has been injected and before returning to the client.

Fallback triggers:

1. Refusal text: `can't answer`, `cannot help`, `Let's talk about something else`, Chinese equivalents.
2. Scope drift: prior-session wording, `correct path` style wording, or absolute paths outside the current planner workspace/evidence.
3. Non-answer placeholders: `Let me first see/check/inspect...`, `我先看看`, etc., when the response has no tool call.

Fallback output is deterministic and based on stored planner evidence. The response metadata records the reason:

```json
{
  "gateway_agent_planner": {
    "synthesis_refusal_fallback": false,
    "synthesis_scope_fallback": true,
    "synthesis_nonanswer_fallback": true
  }
}
```

This closes the observed remote-service failure where `/v1/messages` had already completed `project_analysis` tool evidence collection but chat-only upstream returned a generic refusal or stale workspace answer.

### Integration guard for final synthesis

`tests/integration/agent_planner_synthesis_guard_smoke.py` is the executable architecture contract for final synthesis quality. It uses a fake chat-only upstream and proves that upstream wording cannot override Gateway-owned Planner evidence when the upstream refuses, drifts to another workspace/session, or emits a no-tool placeholder.

## Acceptance runner

`scripts/agent_planner_acceptance.sh` is the operator/developer entry point for validating the architecture after changes. It composes the scenario-specific integration smokes because no single admin query can exercise every runtime mode at once.

- default: smoke-level Agent Planner acceptance;
- `--full`: smoke-level acceptance plus full pytest suite.

This separation keeps live admin audit scoped and truthful while still providing a single command for broad regression proof.

### Gateway-owned compatibility endpoints in acceptance

The architecture separates conversation paths from Gateway-owned endpoints. The acceptance runner now validates both:

- conversation paths must enter strict Agent Planner boundaries;
- Gateway-owned endpoints such as assistants, threads, models, count_tokens, and direct tool/function calls must remain callable, scoped, observable, and must not grant tool authority to the chat-only upstream.

This keeps the remote Agent Planner design from regressing into either extreme: not every public endpoint is a chat turn, but every supported public endpoint still has an explicit runtime owner and verification path.
