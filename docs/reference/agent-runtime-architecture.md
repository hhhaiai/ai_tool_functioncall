# Agent Planner / Agent Runtime 架构

> 最后校准：2026-07-29。本文描述当前远端 Gateway 中的 Agent Planner、工具所有权、多轮状态和可观测性边界。

## 定位

Gateway 不能假设上游模型拥有本地工具权限。上游可以是 chat-only，只负责规划提示下的推理或最终综合；工具执行权由 Gateway runtime 和下游客户端按所有权明确分配。

```text
request
  → tenant/session/workspace scope
  → intent + workflow planner
  → next action
      ├─ Gateway-owned tool: service-side execution
      ├─ downstream-owned tool: native tool request to client
      └─ synthesis: bounded upstream request
  → evidence/result persistence
  → next round or final response
```

## 核心模块

| 模块 | 责任 |
|---|---|
| `gateway_agent_planner.py` | intent/workflow registry、PlannerDecision、session/event store、synthesis preparation |
| `gateway_tool_runtime.py` | request scope、tool extraction、ownership dispatch、多轮 loop、direct call |
| `gateway_streaming.py` | 流式 planner/tool boundary、SSE 事件和断连取消 |
| `gateway_context.py` | memory recall、rollup、压缩和 fan-out |
| `gateway_http_handler.py` | 公开路由、认证、runtime audit/admin endpoints |
| `gateway_builtin_tools.py` | canonical tool registry、handler、risk 和 aliases |

关键入口：

```text
WORKFLOW_REGISTRY
INTENT_REGISTRY
plan_downstream_tool_request()
prepare_upstream_body()
AgentPlannerStore
record_runtime_event()
list_runtime_events()
run_tool_orchestration()
```

## Scope 模型

Planner 数据不使用单一 caller-visible ID 作为全局键。有效 scope 包含：

```text
authenticated downstream tenant/client
+ conversation/planner session
+ resolved downstream workspace
```

workspace 解析优先使用请求显式字段和客户端项目上下文；缺失时进入匿名隔离空间，不使用 Gateway checkout/cwd。

以下数据均按 scope 写入和查询：

- planner sessions；
- runtime events；
- conversation memories；
- tool execution traces/failures；
- project skills 和 plugin discovery。

普通下游请求不能通过 body 中的 `client_id`、workspace selector 或 list-all flag 越权读取其他租户数据。

## Intent 与 Workflow

`INTENT_REGISTRY` 对当前用户意图做有界分类；不会把完整客户端 harness、全局 `CLAUDE.md/AGENTS.md`、hook reminders 或历史注入文本直接当成当前指令。

`WORKFLOW_REGISTRY` 和 transition table 覆盖主要类型：

- project analysis / code search；
- test/build；
- edit/fix loop；
- QA loop；
- generic tool；
- Gateway-owned tool；
- chat-only synthesis。

Planner 每轮只产生有界 next action，并把证据、失败和完成状态写入 session/event store。达到轮数、预算、超时或失败边界时返回明确状态，不无限循环。

## 工具所有权

### Gateway-owned

纯函数、memory、HTTP Actions、MCP 和受控网络/connectors 可以在服务侧执行。执行结果必须来自真实 handler/connector，失败时记录真实错误。

### Downstream-owned

文件、shell、Skill、GUI、local-agent 和调用方私有函数默认只生成下游原生工具请求。Claude Code/Codex 在用户机器执行后把 `tool_result` / `role:tool` / `function_call_output` 回传。

只有显式本地代理配置和对应权限开关同时满足时，Gateway 才执行用户侧工具。

## Chat-only synthesis boundary

对于 chat-only 上游，进入最终综合前：

1. 清理下游工具 schema、`tool_choice` 和 Gateway 内部路由字段；
2. 注入有界的用户目标、已验证证据、工具结果和失败摘要；
3. 记录 `chat_only_synthesis_boundary` runtime event；
4. 明确 `tool_authority_granted=false`；
5. 忽略最终综合阶段再次出现的伪工具调用，避免循环或越权。

streaming 与非 streaming 都经过同一所有权和 scope 规则。

## 多轮执行

```text
round 0: classify + plan
round 1: dispatch tool request or execute Gateway-owned tool
round 2: ingest real tool result
round N: continue if evidence is insufficient
final: bounded synthesis with no unresolved tool request
```

关键稳定性约束：

- 最大工具轮数；
- 单轮和总输出上限；
- shell/process timeout 与 cancel；
- parallel tool 禁止递归；
- 同一调用 ID 的结果关联；
- downstream result 缺失时不伪造；
- client disconnect 取消上游流；
- 首个 SSE 事件后不重放请求。

## 长上下文

Planner 只扫描有界的当前用户目标和压缩上下文。超长历史先 compact/rollup；需要大范围分析时使用 fan-out partial evidence，再做有界综合。

Responses 的 `input` 与 Chat/Messages 的 `messages` 都参与压缩和 memory 注入。多租户 pressure smoke 验证 rollup/recall 不跨 tenant/workspace 泄漏。

## 可观测性

Admin Basic Auth 下可查询：

| 路径 | 内容 |
|---|---|
| `/admin/agent-capabilities.json` | 静态 workflow、intent、ownership 和 runtime capability catalog |
| `/admin/agent-planner.json` | scoped planner sessions |
| `/admin/memories.json` | scoped conversation memories |
| `/admin/agent-runtime-events.json` | scoped runtime events |
| `/admin/agent-runtime-audit.json` | 当前 scope 的 requirement → evidence 映射 |

Audit 只使用已按当前 query scope 过滤的数据，不为“证明成功”扩大到其他租户。

## 失败语义

| 场景 | 行为 |
|---|---|
| planner 无法确定合法 action | 返回有界失败/澄清状态，不随机执行工具 |
| downstream tool result 缺失 | 保持 unresolved，不生成假结果 |
| Gateway connector 不可用 | 结构化 tool failure + failure log |
| 上游拒绝或超时 | 进入上游错误/failover/降级边界 |
| 参数非法 | 客户端 400 或 tool failure，不导致 Gateway crash |
| scope 数据不存在 | audit 显示 missing/current_scope，不借用其他 scope 证据 |

## 验证入口

```bash
python3 -m pytest -q tests/test_gateway.py tests/test_agent_planner_client_context.py
python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/agent_planner_synthesis_guard_smoke.py
python3 tests/integration/agent_planner_remote_pressure_smoke.py
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
python3 tests/integration/agent_planner_protocol_strict_smoke.py
./scripts/agent_planner_acceptance.sh --full
```

完整发布门禁仍使用 `./scripts/ci_gate.sh`。
