# 原生 Tool API 与当前网关差距复盘

更新时间：2026-06-26

## 结论

用户实测对比成立：当前项目不能再按“普通 API 通过兼容层即可等价原生 tool/function call”理解。
现在的实现已经能把部分明确意图转换成下游工具请求，但它仍然主要是 **tool-call shim / gateway adapter**，不是完整的 **外层 Agent Planner**。

真正接近 Claude Code / Codex 原生工具体验，需要把网关升级为外层智能体：

```text
下游请求
  -> Gateway Agent Planner
     -> 识别任务意图
     -> 生成多步计划
     -> 选择 Skill / MCP / Bash / Read / 客户端工具
     -> 维护 todo / step / evidence / memory
     -> 多轮收集证据并压缩
  -> chat-only 上游只负责基于证据做表达、总结、推理
  -> 网关返回协议兼容的 tool_use / function_call / final answer
```

## 用户对比案例

任务：

```text
分析这套项目
```

原生支持工具的 API / Agent 表现：

```text
Skill(codebase-onboarding)
codebase-memory-mcp / context-mode 多次调用
Todo:
  ✔ 分析 Go 项目结构与架构
  ◻ 追踪核心请求流程
  ◻ 总结风险与改进建议
最终返回结构化项目分析
```

旧网关表现：

```text
Bash(find ...)
上游文本：Let me gather the project structure...
误判 LS(path="to")
下游报错：No such tool available: LS
```

这不是“小 bug”，而是架构层差异：

| 能力 | 原生工具 Agent | 当前网关现状 |
|---|---|---|
| 工具选择 | 模型/Agent 原生规划 | 规则 + 上游文本兜底 |
| Skill 触发 | 会先加载 codebase-onboarding | 已补首轮 Skill 偏好，但还不是完整工作流 |
| MCP 使用 | 可主动多次调用 | 目前只透传/表面化，缺少 planner 状态机 |
| Todo/进度 | Agent 内建任务管理 | 缺少等价的 planner state |
| 多轮证据 | 持续调用工具并整合 | 主要依赖下游下一轮回传 |
| 上下文压缩 | Agent 侧长期管理 | 已有 context/memory 基础，但未接入 planner evidence |
| 防幻觉工具 | 原生工具注册约束 | 已修复部分 undeclared tool 问题，但仍需系统化 |

## 当前已经修复的第一层问题

本轮已验证当前本地网关在 `Skill` 可用、且 system 提到 `codebase-onboarding` 时，会直接返回：

```json
{
  "type": "tool_use",
  "name": "Skill",
  "input": {
    "skill": "codebase-onboarding"
  }
}
```

这解决的是“第一步应该先调 Skill，而不是直接 Bash/find”的问题。

但它仍然不是完整解法，因为后续还缺：

1. planner 状态持久化；
2. Skill 执行结果回传后的下一步选择；
3. MCP/context-mode 优先级；
4. todo/progress 管理；
5. evidence compaction；
6. 最终回答前的证据总结 prompt；
7. streaming 路径同等能力。

## 应采用的新架构

新增模块建议：

```text
src/gateway_agent_planner.py
```

核心数据结构：

```text
PlannerIntent
PlannerStep
PlannerState
PlannerEvidence
PlannerDecision
```

核心职责：

1. **Intent Router**
   - 识别 `分析这套项目`、读文件、列目录、搜索代码、运行测试、Web 搜索等任务。
2. **Tool Registry Adapter**
   - 只选择请求体中声明过的工具。
   - 对不同客户端 schema 做参数适配。
3. **Workflow Planner**
   - 项目分析优先级：
     1. `Skill(codebase-onboarding)`
     2. codebase-memory / MCP / context-mode
     3. Read / Glob / Grep
     4. Bash fallback
4. **State Store**
   - 按 session/workspace/request lineage 保存 planner 状态。
   - 记录已完成 step、待执行 step、工具结果摘要、错误。
5. **Evidence Memory**
   - 工具结果超过阈值时压缩旧证据。
   - 只把 compact evidence 给 chat-only upstream。
6. **Final Synthesizer**
   - 上游模型只看证据，不再负责猜测工具调用。

## 最小验收标准

以 `分析这套项目` 为首个验收场景：

1. 首轮：如果下游声明 `Skill` 且可用 `codebase-onboarding`，必须返回 `Skill(codebase-onboarding)`。
2. Skill 结果回传后：必须继续请求至少一个真实代码/结构收集工具，而不是把上游占位文本当最终答案。
3. 不允许生成未声明工具，例如下游没声明 `LS` 时不能返回 `LS(path="to")`。
4. 如果只有 `Bash` 可用，可以返回安全的项目结构命令。
5. 多轮证据足够后，才调用 chat-only upstream 生成最终分析。
6. streaming 与 non-streaming 行为一致。

## 当前风险

- `CLAUDE.md` 中早期“Tool Calls 完整支持”的表述过强，应理解为“协议转换和部分适配已实现”，不是“外层 Agent Planner 已完成”。
- 继续在 `gateway_tool_runtime.py` 里堆正则和特殊分支，会让行为越来越脆；需要拆出 planner 模块。
- 原生工具体验的差距主要不是 JSON 格式，而是 **规划、状态、证据、压缩、验证闭环**。

## 2026-06-26 第一阶段落地

已新增：

```text
src/gateway_agent_planner.py
```

当前具备：

1. `AgentPlannerStore`
   - sqlite 存储位置：`.gateway_runtime/agent_planner.sqlite3`
   - 保存 session/workflow/current_step/evidence_summary/compaction_count。
2. `plan_downstream_tool_request()`
   - 在调用 chat-only upstream 之前决定下一步工具。
   - 项目分析 workflow：
     - `codebase_onboarding`
     - `project_structure`
     - `key_file_read`
     - `synthesis`
   - 通用工具 workflow：
     - `skill_request`
     - `shell_command`
     - `read_file`
     - `list_directory`
     - `web_search`
     - `custom_function`
     - `code_search`
     - `test_build`
     - `edit`
     - `fix_loop`
     - `patch_apply`
     - `qa_loop`
3. `prepare_upstream_body()`
   - 最终 synthesis 前注入 compact evidence。
   - 系统提示明确要求 upstream 不再声称“我将读取/调用工具”，只能基于 evidence 总结。
4. schema 适配
   - 保持 Claude Code `Skill(skill=...)` / `Read(file_path=...)`
   - 保持 Codex `exec_command(cmd=...)`
   - 不向下游发送未声明的工具。
   - 自动补 codebase-memory MCP 的 `project` 参数。
   - 明确编辑请求才下发 `Edit/Write`，避免模糊请求导致意外写入。
   - 上游结构化 patch JSON 会在 gateway 内重新适配 caller-declared schema，避免 text fallback 把 `file_path` 误归一化成客户端不接受的 `path`。
   - Edit/Write 结果回传后按原始测试/修复意图自动下发验证命令。
   - 验证通过后进入 final synthesis，而不是重复下发测试工具。
5. 周期性摘要
   - 每 N 个新工具结果或 evidence 超阈值后尝试 LLM 摘要。
   - LLM 摘要失败时自动回退到 rolling extractive summary。
   - 可通过环境变量关闭或调节：`GATEWAY_AGENT_PLANNER_LLM_SUMMARY`、`GATEWAY_AGENT_PLANNER_SUMMARY_EVERY`、`GATEWAY_AGENT_PLANNER_SUMMARY_TRIGGER_CHARS`。

已验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

本阶段仍未完成：

- streaming 多轮 planner 完整验收；
- 所有复杂任务类型统一迁移到 planner（真实客户端多轮执行验证、更丰富的修复策略）；
- 真实上游下的 LLM 周期摘要压测；
- 真实 Claude Code/Codex 长链路 smoke；
- 真实客户端长链路里验证 MCP project 自动推断与 codebase-memory 调用稳定性。

## 2026-06-26 第二阶段校正：从 shim 到修复闭环 planner

本轮对比真实 tool API 后确认：原生 Agent 的关键不是“模型能不能输出 `tool_use` 字段”，而是 planner 在上游模型之外持续维护任务状态、证据和验证闭环。

已补齐的 planner 行为：

- 失败测试/构建 evidence 进入 `fix_loop` 后，先读诊断文件，不直接让弱上游猜。
- 诊断文件若是测试文件，planner 会继续解析 import 关系，读取被测源码。
- evidence summary 现在包含 tool 输入参数，避免 upstream 只看到内容却不知道文件来源。
- 上游 JSON patch 只在 evidence 完整后进入 `patch_apply`；gateway 再把它适配成调用方声明的 `Edit` schema。
- `Edit` 后自动验证；验证通过后才进入 final synthesis。

本地验收：

```text
Bash(pytest) -> Read(tests/test_app.py) -> Read(src/app.py) -> upstream JSON Edit -> Edit -> Bash(pytest) -> upstream final text
```

实际 smoke 中还包含少量诊断候选 Read，因此记录为：

```text
["Bash", "Read", "Read", "Read", "Read", "Edit", "Bash"], upstream_calls=2
```

这说明当前方向已经从“弱上游文本工具适配器”推进为“gateway 外层 Agent Planner”。下一步不应继续堆更多自然语言正则，而应扩展 planner 的 workflow graph、证据归因、MCP/code graph 优先级、streaming parity 与真实客户端验收。

## 2026-06-26 第三阶段：Planner 自身的无限上下文契约

Agent Planner 不能依赖上游模型上下文来记住工具链路；否则 chat-only upstream 一旦上下文短、请求被压缩，planner evidence 就会消失。

本阶段确立的契约：

1. **稳定 session**：匿名会话以首个真实用户请求为 anchor；显式 metadata 优先。tool_result 不得改变 session key。
2. **先记忆再压缩**：完整工具 evidence 先写入 planner sqlite state。
3. **压缩后再注入**：全局 context compaction 完成后，planner compact summary 必须重新注入最终上游 payload。
4. **双路径一致**：streaming / non-streaming 都遵守同一顺序。

这让“无限上下文”从单纯消息截断升级为：

```text
完整工具结果 -> Planner evidence summary(sqlite, 周期性摘要) -> payload compaction -> planner summary reinjection -> chat-only upstream synthesis
```

当前已用回归测试证明：即使 `context.max_input_tokens` 极小并触发 compaction，上游请求仍保留 `Gateway Agent Planner evidence` 和关键文件证据。

## 2026-06-26 第四阶段：计划工具成为 planner 一等步骤

原生 Agent 的差异不只是“能调工具”，还包括可见的任务计划和进度状态。为此 planner 增加 `planner_progress` 阶段：

```text
user intent -> update_plan/TodoWrite (if declared) -> Skill/MCP/Bash/Read/Edit -> evidence summary -> final synthesis
```

契约：

- 只在下游显式声明计划工具时触发，避免给弱客户端制造未知工具。
- 计划结果回传后，不重复计划，继续执行 workflow 下一步。
- project_analysis 默认计划：加载技能/规则、收集结构/关键文件、压缩证据并综合。
- fix/test 默认计划：运行验证、读取失败源码、应用修复并复验。

这让外层 Agent Planner 更像真正 agent：由 gateway 负责意图、计划、工具调度、证据和验证；chat-only upstream 只负责最终语言合成。

## 2026-06-26 第五阶段：project_analysis 优先使用代码图谱

原生工具 Agent 在项目分析时通常优先走 Skill/MCP/code graph，而不是直接 Bash 枚举文件。Planner 现在将结构收集优先级固定为：

```text
get_architecture -> search_graph -> search_code -> LS/Glob -> Bash fallback
```

契约：

- 只调用下游声明过的 MCP/code graph 工具。
- 自动补齐 codebase-memory 的 `project` 参数。
- 当 code graph 不可用时保留旧的 LS/Glob/Bash fallback。

这进一步把 gateway 从“弱上游工具适配器”推进为“外层项目理解 agent”。

## 2026-06-26 第六阶段：streaming parity 验证

外层 Agent Planner 的 evidence/无限上下文契约必须同时适用于 streaming。新增 streaming scoped 回归后，当前契约为：

```text
streaming request with tool_result -> persist full planner evidence -> compact payload -> reinject planner summary -> upstream non-streaming synthesis -> downstream SSE final response
```

本阶段修复了 `_run_streaming_orchestration_scoped()` 中 planner import 作用域问题，并验证 context compaction 后 planner evidence 仍然存在。

## 2026-06-26 第七阶段：从结构收集推进到核心流程追踪

原生 coding agent 做项目分析时不会停在 `find/LS/get_architecture`，而会继续定位入口、路由、handler、服务调用链。为缩小这个差距，Agent Planner 新增 `core_flow_trace` 阶段：

```text
update_plan/TodoWrite
  -> Skill(codebase-onboarding)
  -> project_structure(get_architecture/search_graph/search_code/LS/Bash)
  -> core_flow_trace(search_graph/search_code/Bash grep)
  -> key_file_read
  -> synthesis
```

新增契约：

1. Planner 生成的工具 id 采用 `planner_<step>_<uuid>`；tool_result 回来后解析并持久化到 `completed_steps`。
2. 只有 `project_structure` 确认为 planner-managed 已完成后，才自动追加 `core_flow_trace`，避免对用户手写/历史工具结果过度调度。
3. `core_flow_trace` 优先级：
   - `mcp__codebase_memory_mcp__search_graph` / `search_graph`
   - `mcp__codebase_memory_mcp__search_code` / `search_code`
   - `Bash`/`exec_command` grep fallback
4. 仍然遵守 tool registry：未声明的工具不下发，schema 参数继续由 gateway 适配。

验证点：

```bash
python3 -m pytest \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_records_completed_steps_from_planner_tool_ids -q
# 2 passed
```

这一步把项目分析从“收集结构证据”推进为“按 workflow 状态继续追核心流程”。后续应继续把 `trace_path`、`get_code_snippet`、真实 Claude/Codex 长链路 smoke 纳入同一 workflow graph。

## 2026-06-26 第八阶段：符号级实现证据 deep-dive

`core_flow_trace` 只能告诉 planner 哪些入口/handler/编排函数重要；如果直接让 chat-only upstream 总结，仍可能基于搜索摘要猜实现。为此新增 `symbol_deep_dive` 阶段：

```text
core_flow_trace(search_graph/search_code)
  -> parse qualified_name
  -> get_code_snippet(qualified_name)
  -> trace_path(function_name, direction=both, mode=calls)
  -> key_file_read / synthesis
```

契约：

1. 只从真实 tool evidence 中解析 `qualified_name`，不从 upstream prose 中猜。
2. `get_code_snippet` 优先于普通 Read，因为它可以携带符号上下文和邻近关系。
3. `trace_path` 使用 qualified name 的 leaf function，默认 `direction=both`、`mode=calls`、`depth=2`，用于补调用链证据。
4. 如果下游没有声明这些 codebase-memory 工具，则跳过该阶段并继续 key file read / synthesis，不生成未知工具。
5. 参数继续由 gateway schema adapter 自动补 `project`，避免真实客户端 schema 不一致。

新增验收：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace -q
# 1 passed
```

这一步让外层 planner 更接近原生 Agent 的行为：不是只“找文件”，而是围绕重要符号读取源码并追调用链。

## 2026-06-26 第九阶段：Streaming 多轮 planner 调度闭环

之前 streaming 只验证了 direct tool request 和 evidence compaction；真正的外层 Agent Planner 还必须在 stream 模式下跨多轮继续调度。新增验收场景：

```text
/v1/messages stream=true
  user: 分析这套项目
  -> message SSE: tool_use Skill(codebase-onboarding)

/v1/messages stream=true
  tool_result: Successfully loaded skill
  -> message SSE: tool_use search_graph(project architecture)

/v1/messages stream=true
  tool_result: Architecture evidence
  -> message SSE: tool_use search_graph(core request flow)
```

契约：

1. streaming direct planner response 不调用 chat-only upstream。
2. tool_result 回来后，planner 继续读取 state/evidence 并选择下一步工具。
3. 输出仍是下游原生 SSE tool_use/message_delta/message_stop，而不是 gateway 私有格式。
4. 与 non-streaming workflow 保持一致：Skill -> project_structure -> core_flow_trace。

新增测试：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools -q
# 1 passed
```

这补齐了 streaming “工具调度链路”的重要验收；后续仍需真实 Claude/Codex 长链路 smoke，以及 streaming 场景下 `symbol_deep_dive`/最终 synthesis 的更完整端到端覆盖。

## 2026-06-26 第十阶段：Streaming symbol deep-dive 后最终综合

补齐 streaming 的另一半闭环：当 project_analysis 已经完成结构收集、核心流程追踪、符号源码 deep-dive 后，planner 必须停止继续发工具，并把压缩证据交给 chat-only upstream 做最终表达。

新增验收：

```text
completed steps:
  project_structure
  core_flow_trace
  symbol_deep_dive

streaming path:
  full evidence -> planner sqlite summary
  -> upstream context compaction
  -> reinject planner evidence
  -> upstream final synthesis
  -> downstream SSE final response
```

契约：

1. `symbol_deep_dive` 完成后，如果没有新的必要工具，进入 synthesis。
2. synthesis 不是把原始长上下文直接丢给上游，而是使用 planner evidence summary。
3. 即使 context compaction 发生，最终 upstream payload 仍必须包含 `Gateway Agent Planner evidence` 和核心符号证据。
4. streaming 输出应是最终文本 SSE，不应再出现 `stop_reason=tool_use`。

新增测试：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 1 passed
```

这和第九阶段的 streaming 多轮调度测试形成闭环：

```text
streaming: next tool scheduling ✅
streaming: final evidence synthesis after tools ✅
```

## 2026-06-26 第十一阶段：真实 CLI smoke 暴露的 one-shot 工具循环

真实 Codex CLI 验证发现一个 mock 单元测试不容易覆盖的问题：Responses 协议下，Codex 执行 `exec_command` 读取文件后，会把结果作为 `function_call_output` 回传；如果 planner 只看原始 user prompt 的 `Read path` 意图，就会再次下发同一个读文件工具，形成循环。

修复契约：

1. explicit one-shot 工具请求包括：Skill、shell command、read file、list directory。
2. 一旦当前请求已包含任何 tool evidence，planner 不再重复下发同一个 one-shot 工具。
3. 后续转入 `prepare_upstream_body()`，把 tool evidence 注入 chat-only upstream 做最终表达。
4. 这不影响多步 workflow：测试修复、项目分析等仍由 workflow state / completed_steps 驱动继续调度。

同时修复 Responses SSE usage 兼容：`response.completed.response.usage` 现在补齐 `input_tokens/output_tokens/total_tokens`，避免 Codex 解析 completed event 时失败。

真实验证：

```bash
python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true
# claude.ok=true
# codex.ok=true
```

这一步很关键：它证明外层 planner 不仅在 mock 中可用，也能适配真实 Claude/Codex CLI 的工具回传形态和流式 Responses 解析要求。

## 2026-06-26 第十二阶段：完整 project_analysis 集成 smoke

新增 `tests/integration/agent_planner_project_analysis_smoke.py`，把前面分散的单元能力串成完整项目分析链路：

```text
update_plan
  -> Skill(codebase-onboarding)
  -> search_graph(project architecture)
  -> search_graph(core request flow)
  -> get_code_snippet(qualified_name)
  -> trace_path(function_name)
  -> Read(key files)
  -> final synthesis
```

该 smoke 的 fake upstream 不会产生任何工具调用，只返回最终文本；所有工具选择都必须来自 Gateway Agent Planner。验收点：

1. 工具顺序必须符合项目分析 workflow。
2. codebase-memory 参数必须自动补 `project`。
3. `core_flow_trace` evidence 中的 `qualified_name` 必须驱动 `symbol_deep_dive`。
4. 最终 upstream synthesis prompt 必须包含：
   - `Gateway Agent Planner evidence`
   - 核心 qualified name
   - key file Read evidence marker
5. 最终只调用 chat-only upstream 一次。

本阶段还修复一个实际 evidence 注入问题：

- memory recall 会在消息前插入 `[Gateway recalled memory]`，如果 session anchor 取到该消息会导致 planner state 漂移。
- Anthropic messages 转 OpenAI Chat 时多个 system 消息原来是后者覆盖前者，可能丢掉 planner evidence prompt。

修复后：

- planner session anchor 跳过 recalled memory / planner evidence 注入噪声；
- system messages 转换时合并而不是覆盖。

验证：

```bash
python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1
```

## 2026-06-26 回归收口补充：差距点不是格式，而是工具所有权边界

用户再次对比真实 tool-capable Agent 后，差异的核心进一步明确：外层 planner 不能只“看到工具名就发 tool_call”，必须先判断这个工具到底由谁执行。

本轮修复两个反例：

1. `get_weather` 是 Gateway HTTP Action 时，planner 不能当作 caller-private custom function 下发给客户端；否则 Gateway-owned action 不执行，upstream 也拿不到 tool result。
2. 普通 no-tools chat 请求即使文本里有“分析这套项目”，planner 也不能发明 `Glob/LS`；没有声明工具时必须保留普通 upstream/context fanout 行为。

验证已补到全量：

```bash
python3 -m pytest -q
# 923 passed, 2 skipped
```

这次收口后的边界规则：

| 工具类型 | 执行方 | Planner 行为 |
|---|---|---|
| Read/Bash/Skill/本地文件/客户端机器工具 | 下游客户端 | 可以 surface protocol-level tool request |
| HTTP Action / MCP Gateway connector | Gateway | 不抢走，交给 Gateway orchestration 执行并 round-trip |
| 请求未声明任何工具 | 无工具面 | 不生成 synthetic tool，走普通 upstream/context/fanout |

结论仍然保持：真正差距不是 `tool_calls` JSON 字段，而是 planner 的任务状态、证据链、工具所有权、上下文压缩和验证闭环。当前实现已向这个方向推进，并通过本地全量回归，但复杂真实任务仍需要继续扩展 workflow 策略。

## 2026-06-26 无限上下文实现补充

外层 Agent Planner 的“无限上下文”至少分两层：

1. **Planner evidence memory**：工具结果、步骤、修复证据周期性压缩，最终 synthesis 前注入。
2. **Conversation memory**：普通长对话历史在上游窗口不足时压缩为 previous summary + recent messages。

本轮补齐第二层：

- Chat Completions：旧 messages -> `[Previous conversation summary]`，recent messages 保留。
- Anthropic Messages：summary 放入 `system` 字段，避免向 messages 数组塞 system role。
- Responses：`input` list 同样按 summary + recent 压缩。
- LLM 摘要失败时使用 extractive digest 兜底，不再只有“context was too long”的占位符。

这意味着 chat-only 上游现在的职责更加接近“只做表达/综合”：它接收的是外层 Agent Planner 与 context manager 已经整理过的 compact evidence，而不是被迫处理无限膨胀的原始工具/历史 payload。

## 2026-06-26 Gateway-owned 工具职责前移

外层 Agent Planner 继续从 shim 变成真正调度者：

- 之前：HTTP Action 这类 Gateway-owned 工具仍可能需要 chat-only upstream 输出 `<function=...>` 文本，再由 Gateway 解析执行。
- 现在：明显匹配的 Gateway-owned tool 由 planner 在 upstream 前预执行；tool result 注入上下文；upstream 只做最终回答。

这把工具所有权边界进一步明确：

| 工具类型 | 当前行为 |
|---|---|
| Downstream user-machine tools | surface 给 Claude/Codex 客户端执行 |
| Gateway-owned HTTP Action / connector | planner 可预执行并注入结果 |
| Chat-only upstream | 不再承担这些工具的调用格式生成，只做 synthesis |

这仍不是最终完整 Agent Runtime，但已经从“工具格式兼容”推进到“功能调度职责迁移”。

## 2026-06-26 Streaming parity 补充

外层 Agent Planner 不能只在非流式请求里工作；否则真实客户端一开 stream 就会回到“弱上游猜工具调用”的旧架构。

本轮已把 Gateway-owned preexecute 接入 streaming：

```text
streaming request
  -> direct local/downstream checks
  -> planner preexec Gateway-owned HTTP/MCP tool
  -> append tool result
  -> compact/inject planner evidence
  -> chat-only upstream final synthesis
  -> Gateway emits SSE
```

这使 sync 与 stream 的职责边界一致：工具调度在 planner，chat-only upstream 做表达。

## 2026-06-26 Service-side registry 补充

原生 Agent 的一个关键特征是：工具注册表属于 Agent runtime，而不是完全依赖用户每次请求传入工具列表。

本轮补齐 HTTP Action registry：

```text
Gateway config.http_actions
  -> Agent Planner service-side capability registry
  -> intent match + argument inference
  -> preexecute HTTP Action
  -> inject tool result
  -> chat-only upstream synthesis
```

这意味着普通 chat 请求只要命中配置好的 Gateway-owned action，就可以由 planner 调度服务侧能力；客户端不必显式声明 `tools`。同时，plain chat/fanout 回归保持通过，说明该能力仍受意图匹配约束。

## 2026-06-26 MCP registry 补充

Planner registry 继续从 HTTP Action 扩展到 MCP connector：

```text
Gateway config.mcp.servers
  -> tools/list discovery
  -> mcp__server__tool public names
  -> intent match + argument inference
  -> preexecute MCP tool
  -> inject tool result
  -> chat-only upstream synthesis
```

这一步很关键：原生 Agent runtime 的工具集不是每轮由用户请求硬塞进来的，而是 runtime 自己维护并按任务选择。当前 Gateway 已开始具备这个形态：配置型 MCP server/tool 成为 planner 的 service-side capability。

## 2026-06-26 可观测性差距：最终响应必须暴露 Planner 轨迹

真实 tool-capable Agent 的一个明显差别是：调用方能从响应结构、tool_use/tool_result、trace 或 runtime metadata 看出“模型/运行时到底做了什么”。单纯 chat-only adapter 即使内部预执行了工具，如果最终响应不带任何 planner 元数据，调试体验仍像黑盒。

本轮补齐一层外层 runtime 观测面：

```text
planner preexecute HTTP Action / MCP
  -> append evidence
  -> chat-only upstream synthesis
  -> final response.gateway_context.agent_planner
```

目前最终响应会携带白名单 `gateway_context` 字段：

- `agent_planner`
- `local_planner`
- `planner_evidence_chars`
- `compacted`
- `strategy`

这不能替代原生模型推理能力，但能把 Gateway 的 Agent Planner 行为显式暴露出来：客户端/日志/测试可以确认本轮是 plain chat、downstream tool request，还是 Gateway-owned service-side workflow。

## 2026-06-26 Built-in 工具注册表差距

原生 Agent runtime 的工具注册表通常包含两类能力：外部 connector（MCP/HTTP Action）和 runtime 自带工具（计算、时间、搜索、记忆等）。如果 Gateway 只把 HTTP Action/MCP 前移，而 calculator/time/search 仍交给 chat-only upstream 生成格式，本质上还没有把“功能调度”完整收回到 planner。

本轮开始补 built-in service registry：

```text
user intent: Calculate 6*7
  -> planner detects arithmetic intent
  -> service-side calculator executes
  -> tool result appended to context
  -> chat-only upstream synthesizes final wording
```

边界保持不变：

- service-side：`calculator`、`current_time`、`WebSearch` 等非 user-machine 能力。
- downstream-owned：Read/Write/Edit/Bash/GUI/local agent 等必须运行在用户真实机器/项目上下文的能力。

这使 Gateway 更接近“Agent 提供意图解析 + 功能调度”，而不是“上游模型自己决定工具格式”。

## 2026-06-26 无限上下文差距：不能只在超限时临时压缩

真正的外层 Agent Runtime 需要自己维护长期上下文，而不是等上游报 too long 后才删历史。否则 chat-only 模型仍然承担“记住整个任务”的职责。

本轮新增 periodic session rollup：

```text
each completed turn
  -> compact turn memory into SQLite
  -> every N turns build [Periodic conversation summary]
  -> store kind=session_rollup
  -> future requests prepend latest rollup before ordinary recalled memories
  -> chat-only upstream receives compact long-term context
```

配置：

- `context.memory_rollup_every_turns` / `GATEWAY_MEMORY_ROLLUP_EVERY_TURNS`，默认 8。
- `context.memory_rollup_max_chars` / `GATEWAY_MEMORY_ROLLUP_MAX_CHARS`，默认 4000。
- `GATEWAY_MEMORY_ROLLUP_LLM_SUMMARY=1` 可启用 LLM rollup；默认 extractive fallback，保证 summary upstream 不可用时仍然可以持续滚动总结。

这把“无限上下文”从被动压缩推进为主动长期记忆维护。

## 2026-06-26 远端服务形态修正：不是本地增强服务

外层 Agent Planner 的关键前提：Gateway 是远端多租户 runtime。它不能把服务机 cwd 当用户项目目录，也不能因为两个用户 prompt 相同就共享 anonymous workspace / planner session / memory。

本轮修正的边界：

```text
client request
  -> resolve client workspace from explicit field / client metadata
  -> if absent, create isolated anonymous per-request or tenant-session workspace
  -> workspace ContextVar scopes tool execution per request/thread
  -> planner_session_key includes tenant + workspace + session
  -> memory_session_key includes tenant + session
```

原则：

- User-machine tools：Read/Edit/Bash/Skill/GUI/local agent 默认下发给 client，在 client workspace 执行。
- Gateway-owned service tools：HTTP Action/MCP service connector/calculator/current_time/WebSearch 可由服务端 planner 执行。
- Planner/memory/workspace state：必须按 tenant/session/workspace 隔离，不能只按 prompt 或裸 session_id 隔离。

这一步是从“本地代理增强”转向“远端 Agent Planner 服务”的必要架构修正。

## 2026-06-26 远端 runtime state 差距：裸 id 不能做全局 key

原生本地 agent runtime 可以假设一个进程主要服务一个用户/一个 workspace；远端 Agent Planner 不能这样假设。即使工具名和协议 id 看起来像本地会话，服务端也必须认为它们来自多租户并发请求。

修正后的模型：

```text
client request body
  -> resolve client workspace
  -> derive runtime scope = tenant/user + session/conversation + workspace hash
  -> public tool id stays unchanged for protocol compatibility
  -> internal state key = runtime scope + public id
```

覆盖范围：

- exec shell sessions
- background Gateway agent sessions
- lightweight team mailboxes
- pending user-input requests

这解决了远端场景中的关键不稳定点：两个用户同时使用相同 `session_id` / `team_id` 不会再覆盖、等待、写入或删除彼此的进程内状态。匿名无 session 请求使用 per-request runtime scope，宁可不支持跨请求复用，也不能因为 prompt 或裸 id 相同而共享状态。

## 2026-06-26 Streaming observability gap

外层 Agent Planner 如果只在非流式响应中暴露 `gateway_context`，流式客户端看到的仍像普通 chat-only upstream 文本，无法确认 planner 是否执行、执行到哪个 step、是否使用了压缩 evidence。这不符合远端 Agent Runtime 的可观测性要求。

本轮采用兼容优先方案：

```text
non-streaming final response
  -> response.gateway_context

streaming final response
  -> existing terminal/completed SSE payload + gateway_context
```

具体位置：

- Chat Completions final chunk: top-level `gateway_context`
- Anthropic Messages `message_delta`: top-level `gateway_context`
- OpenAI Responses `response.completed.response.gateway_context`

刻意没有新增 `event: gateway_context`：不少 SDK 会按官方 stream schema 解析 data payload，自定义 event 虽然符合 SSE，但可能破坏严格客户端。把 metadata 附在已有终止/完成 payload 上，更适合远端服务渐进兼容。

## 2026-06-26 State snapshot gap

真正的外层 Agent Planner 不能只输出“下一步工具调用”。远端多用户 runtime 必须能回答：当前 workflow 是什么、已经完成哪些步骤、收集了多少证据、证据是否被压缩、最终 synthesis 使用了哪份证据。否则它仍然像一个兼容 gateway，而不是可观测 agent runtime。

本轮增加 bounded state snapshot：

```json
{
  "workflow": "project_analysis",
  "current_step": "core_flow_trace",
  "completed_steps": ["project_structure"],
  "evidence_count": 2,
  "evidence_summary_chars": 1234,
  "compaction_count": 1,
  "llm_compaction_count": 0,
  "session_key": "...",
  "evidence_summary_preview": "..."
}
```

传播位置：

- downstream tool request: `response.gateway_context.agent_planner.state`
- final synthesis request: `request.gateway_context.agent_planner.state`
- final synthesis response/stream: 通过既有 `gateway_context` propagation 返回给客户端

这样后续可以自然扩展出 Agent Planner UI、admin workflow status endpoint、telemetry events，而不是只能从服务端 sqlite 或日志反查。

## 2026-06-26 Admin runtime status gap

只有响应内 `gateway_context.agent_planner.state` 还不够。远端服务需要一个服务端 status surface，让 admin/运维/未来 UI 能查询最近 planner sessions，而不是去翻 sqlite 或日志。

新增 endpoint：

```http
GET /admin/agent-planner.json?limit=50
Authorization: Basic ...
```

返回 bounded snapshots：

```json
{
  "sessions": [
    {
      "workflow": "project_analysis",
      "current_step": "codebase_onboarding",
      "completed_steps": [],
      "evidence_count": 0,
      "evidence_summary_chars": 0,
      "compaction_count": 0,
      "llm_compaction_count": 0,
      "session_key": "...",
      "updated_at": 1234567890.0
    }
  ]
}
```

这是从 gateway shim 向远端 agent runtime 迈进的关键可操作性补充：planner state 不只在单次响应里可见，也可以被服务端状态面板/API 查询。

## 2026-06-26 Admin filtering gap for remote multi-user service

远端 Agent Planner 不能只提供“最近 session 列表”。多用户同时请求时，admin/运维最常见的问题是：某个 tenant、某个 session、某类 workflow 是否正常推进。如果只能返回最近 N 条，排障会依赖服务端日志/SQLite 手工查询，也容易混淆不同 client workspace。

本轮补齐最小只读过滤面：

```http
GET /admin/agent-planner.json?limit=50&workflow=project_analysis&current_step=codebase_onboarding&session_contains=abc&tenant_contains=tenant-a&has_evidence=1
Authorization: Basic ...
```

响应仍然是 bounded snapshot，不暴露完整 evidence：

```json
{
  "sessions": [
    {
      "workflow": "project_analysis",
      "current_step": "codebase_onboarding",
      "evidence_count": 0,
      "session_key": "...tenant:tenant-a:session:abc...",
      "updated_at": 1234567890.0
    }
  ],
  "filters": {
    "workflow": "project_analysis",
    "current_step": "codebase_onboarding",
    "session_contains": "abc",
    "tenant_contains": "tenant-a",
    "has_evidence": true
  },
  "limit": 50
}
```

设计边界：当前 `tenant_contains` 仍基于 `session_key` substring，因为现有 SQLite schema 只有 `session_key/state_json/updated_at`。这保持迁移风险最低。下一步如果要更强的远端运维能力，可以给 `planner_sessions` 增加独立 `tenant_key/workspace_key/workflow/current_step` columns，提升查询效率并减少对字符串格式的依赖。

## 2026-06-26 Store indexing gap

上一轮 admin filters 解决了“能查”的问题，但如果底层仍只靠 `session_key` 字符串 contains，就不适合长期远端多租户运行：

- session key 格式变更会影响查询；
- tenant/workspace/workflow/current_step 没有 SQL index；
- Admin UI / telemetry 很难做稳定过滤；
- 旧记录迁移和新记录写入没有统一索引面。

本轮把 `planner_sessions` 从兼容 KV 表升级为 indexed runtime table：

```sql
planner_sessions(
  session_key TEXT PRIMARY KEY,
  tenant_key TEXT NOT NULL DEFAULT '',
  workspace_key TEXT NOT NULL DEFAULT '',
  workflow TEXT NOT NULL DEFAULT '',
  current_step TEXT NOT NULL DEFAULT '',
  evidence_count INTEGER NOT NULL DEFAULT 0,
  state_json TEXT NOT NULL,
  updated_at REAL NOT NULL
)
```

查询层仍保留原 API：

```http
GET /admin/agent-planner.json?tenant_contains=tenant-a&workflow=project_analysis&has_evidence=1
```

但底层现在走 SQL WHERE + index，而不是 Python 扫描最近列表。旧 sqlite 表会自动补列，并 bounded backfill 最近旧 session 的索引字段。这样 Agent Planner 更像一个可运维的远端 runtime，而不是只在单次请求里临时拼接状态的 gateway shim。

## 2026-06-26 Infinite context memory indexing gap

Agent Planner 负责“无限上下文”时，周期总结不能只是把摘要塞进 SQLite。远端服务必须保证：

- 同一个 tenant/session 才能 recall；
- 同一个 client workspace 才能 recall；
- periodic rollup 只汇总当前 scope 的对话；
- 旧 memory rows 能迁移，不丢已有上下文；
- admin/debug surface 能看到 scope 字段。

本轮把 `conversation_memories` 从旧的 `session_key + workspace_root` 查询升级成显式 scope：

```sql
conversation_memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  session_key TEXT NOT NULL,
  workspace_root TEXT NOT NULL,
  tenant_key TEXT NOT NULL DEFAULT '',
  workspace_key TEXT NOT NULL DEFAULT '',
  memory_session_key TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  keywords_json TEXT NOT NULL DEFAULT '[]',
  source_request_id TEXT,
  importance INTEGER NOT NULL DEFAULT 1,
  last_used_at TEXT
)
```

核心查询现在基于：

```text
tenant_key + workspace_key + memory_session_key
```

覆盖对象包括普通 recall、smart search、latest rollup、recent-since-rollup。这样“隔一段时间总结一下”的无限上下文能力更接近远端 Agent Runtime：总结和回忆都是 scope-aware，而不是本地单用户进程里的松散缓存。

## 2026-06-26 Memory observability gap

无限上下文如果只能内部 recall，而不能被 admin 按 scope 查询，远端部署仍然难排障：用户说“它记错了/串了/没总结”，服务端需要直接确认 memory/rollup 是否属于正确 tenant/workspace/session。

本轮补齐：

```http
GET /admin/memories.json?tenant_contains=user-a&workspace_contains=project-a&session_contains=session-a&kind=session_rollup
Authorization: Basic ...
```

支持 filters：

```text
tenant_contains
workspace_contains
session_contains
kind
has_rollup=1|0
limit=1..500
```

响应返回每条 memory 的显式 scope：

```json
{
  "tenant_key": "user-a",
  "workspace_key": "/client/project-a",
  "memory_session_key": "session:abc",
  "kind": "session_rollup",
  "summary": "..."
}
```

这让“隔一段时间总结一下”的能力从黑盒变成可观测 runtime 能力：不仅能总结，还能证明总结属于哪个用户、哪个 client workspace、哪个会话。

## 2026-06-26 Unified runtime status gap

Planner status 和 Memory status 分开查询仍然不够。真正的远端 Agent Runtime 需要一个统一状态面，回答同一个问题：某个 tenant/session/workspace 当前 agent 在做什么、证据收集到哪里、无限上下文是否已总结。

本轮新增：

```http
GET /admin/agent-runtime.json?tenant_contains=user-a&session_contains=session-a&workflow=project_analysis&has_rollup=1
Authorization: Basic ...
```

返回结构：

```json
{
  "runtime": {
    "agent_planner": {
      "sessions": [],
      "session_count": 0,
      "active_workflows": []
    },
    "memory": {
      "memories": [],
      "memory_count": 0,
      "rollup_count": 0
    }
  },
  "filters": {},
  "limit": 50
}
```

这个 API 把 gateway 的可观测模型进一步推向 agent runtime：planner workflow/evidence 与 infinite-context memory/rollup 可以用同一组 scope filters 关联查看，而不是各查各的日志。

## 2026-06-26 Runtime event timeline gap

Snapshot API 只能看到当前 planner/memory 状态。真正的远端 Agent Runtime 还需要 timeline：

- planner 什么时候切换 workflow/step；
- evidence_count 什么时候变化；
- memory rollup 什么时候生成；
- 这些事件属于哪个 tenant/workspace/session；
- admin 能否按事件类型检索。

本轮新增 `runtime_events`：

```sql
runtime_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  session_key TEXT NOT NULL DEFAULT '',
  tenant_key TEXT NOT NULL DEFAULT '',
  workspace_key TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL,
  workflow TEXT NOT NULL DEFAULT '',
  step TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
)
```

新 API：

```http
GET /admin/agent-runtime-events.json?tenant_contains=user-a&event_type=memory_rollup
Authorization: Basic ...
```

`/admin/agent-runtime.json` 也会返回最近 events。这样 Agent Runtime 的可观测性从“当前状态”扩展到“状态变化轨迹”。

## 2026-06-26 Runtime observability gap：Gateway-owned 与 fallback dispatch 也必须进 timeline

复盘最新约束：服务形态是远端 Agent Planner Runtime，不是本机 Codex/Claude 的增强层。因此稳定性不只看“能不能返回 tool_use”，还要看多用户并发下每条执行路径是否能按 tenant/session/workspace 追踪。

发现的缺口：

- Planner-managed workflow 已经有 `planner_state` / `tool_dispatch` / `tool_result` / `evidence_compaction`。
- Memory rollup 已经有 `memory_rollup`。
- 但 Gateway-owned service tool preexecute（HTTP Action / MCP / calculator / current_time / WebSearch）缺少 execute/result event。
- 非 planner fallback downstream tool request（例如无声明工具但明显需要 client workspace 的兼容路径）缺少 dispatch event。

修正：

- 在 `src/gateway_tool_runtime.py` 增加 request-scope runtime event helper，复用 `planner_session_key()` 的 tenant/session/workspace 规则。
- Gateway-owned preexecute 写入：
  - `gateway_tool_execute`
  - `gateway_tool_result`
- fallback downstream dispatch 写入：
  - `tool_dispatch`，workflow=`direct_downstream_tool_request`，step=`surface_user_side_tools`
- 事件 metadata 只保存 tool 名称、call id、参数/结果摘要长度和成功失败，不把完整长 evidence 当作事件主体。

验收标准：

- Gateway-owned 工具由服务端执行，但 event 必须按远端请求 scope 归属。
- 用户机器工具仍不得在 Gateway 服务机执行；fallback 只返回 protocol-level tool request 给 client。
- Admin `/admin/agent-runtime-events.json` 可按 tenant/session/workspace/workflow/event_type 查到上述事件。

验证：

```bash
python3 -m py_compile src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_direct_downstream_fallback_records_remote_runtime_event -q
# 2 passed
```

## 2026-06-26 Admin UI observability gap：runtime timeline 不能只存在于 JSON API

远端 Agent Runtime 的稳定性要求不只是内部记录事件，还要让运维/调试者能快速看到 timeline。之前 `/admin/agent-runtime-events.json` 已经可查询，但 Admin UI 只给链接提示，真实排障仍要手动 curl。

本轮修正：

- Admin UI 直接读取最近 runtime events。
- “Agent Runtime Events” 表格展示：时间、事件类型、workflow、step、tenant、workspace、summary。
- 覆盖事件类型包括 planner state、tool dispatch/result、Gateway-owned execute/result、memory rollup、fallback downstream dispatch。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_ui_renders_agent_runtime_events_table -q
# 1 passed
```

意义：多用户远端服务出现“某个用户说工具没跑/上下文丢了/下发到了错误 workspace”时，可以直接从 Admin UI 的 timeline 看事件归属，而不是依赖本地日志猜测。

## 2026-06-26 Infinite context / Planner boundary gap：历史 memory 不能成为当前工具指令

发现的边界问题：为了让 chat-only upstream 能做最终 synthesis，Gateway 会把 recalled memory 注入消息；但 Agent Planner 在 upstream 之前运行，如果直接用注入后的 user content 做 intent/路径判断，就可能把旧 rollup 当成当前用户命令。

风险示例：

- recalled memory: `上次要求读取 OLD.md 并分析项目`
- 当前用户: `hi`
- 错误行为：Planner 看到旧 memory 后下发 Read/分析工具。

修正原则：

- 历史 memory 是 evidence/context，不是当前 instruction。
- Planner 的 intent parsing、路径提取、workflow selection 必须剥离 `[Gateway recalled memory]` 块。
- 原始 body 不变，memory 仍进入最终 synthesis。

实现：

- `_strip_recalled_memory_blocks()` 只在 planner 内部使用。
- `plan_downstream_tool_request()` 和 `_generic_intent_decision()` 使用 sanitized text。
- 测试覆盖：
  - 当前 `hi` 不被旧 memory 触发工具。
  - 当前 `README.md` 优先于旧 `OLD.md`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_recalled_memory_for_current_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_current_request_over_recalled_memory_paths -q
# 2 passed
```

## 2026-06-26 Planner state identity gap：recalled memory 不能改变匿名 session anchor

上一节解决的是 intent parsing；本节解决 state identity。匿名请求没有 explicit `session_id` / `conversation_id` 时，Planner 会用当前请求文本生成 anon session key。无限上下文注入后，如果 anchor 包含 recalled memory，则同一当前请求会因为历史 rollup 内容不同生成不同 session key。

影响：

- planner state 漂移；
- evidence_summary 无法累积到同一 workflow；
- runtime_events timeline 归属不稳定；
- 多轮 project_analysis / fix_loop 可能断链。

修正：

- `_planner_anchor_text()` 在所有入口剥离 `[Gateway recalled memory]` block。
- `/v1/messages` content list 中“memory block + 当前请求”的场景不会再跳过当前请求。
- `/v1/responses` string input 同样剥离 memory block。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_ignores_recalled_memory_anchor_noise \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_responses_session_key_ignores_recalled_memory_anchor_noise -q
# 2 passed
```

## 2026-06-26 Chat-only synthesis boundary gap：最终上游请求不能再带工具

差距：Agent Planner 已经在外层完成工具调度后，最终 synthesis 阶段仍调用 `_merge_builtin_tools()`。对 chat-only upstream，这会把工具 schema 或 text adapter manual 再次注入给上游，破坏“模型只负责对话表达”的职责边界。

修正：

- weak/chat-only upstream 最终 synthesis request 使用 `_chat_only_synthesis_body()`。
- 移除：
  - `tools`
  - `tool_choice`
- 标记：
  - `gateway_context.chat_only_synthesis=true`
  - `gateway_context.upstream_tools_stripped=true`
- native-capable / non-chat-only upstream 保持原 `_merge_builtin_tools()` 行为。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis -q
# 1 passed
```

意义：外层 Agent Planner 成为工具/能力调度唯一责任面；chat-only 模型只接收 memory、planner evidence 和用户对话，生成最终用户可读回答。

## 2026-06-26 Streaming synthesis boundary gap：streaming 也不能把工具交给 chat-only 上游

上一节修复了 non-streaming final synthesis 的工具剥离；本节补齐 streaming path。`_run_streaming_orchestration_scoped()` 原先仍会 `_merge_builtin_tools()`，导致 streaming 最终 synthesis 请求可能携带工具 schema 或 text adapter 手册。

修正：

- streaming weak/chat-only path 使用 `_chat_only_synthesis_body()`。
- 保留 Planner evidence / memory / compacted context。
- 移除 `tools/tool_choice`。
- downstream SSE 仍由 Gateway 负责输出，不依赖 upstream stream。

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 1 passed
```

意义：无论请求是否 stream，chat-only upstream 都只承担最终表达；工具调度统一由外层 Agent Planner Runtime 负责。

## 2026-06-26 Final synthesis tool-authority gap：剥离 tools 还不够，还要禁止解析上游伪工具

上一节只保证 final synthesis request 不给 chat-only upstream `tools/tool_choice`。但旧 runtime 在收到上游响应后仍会解析：

- native-like tool calls
- text tool calls
- intent fallback phrases

因此弱模型只要输出 JSON `Edit` 或工具标签，仍可能重新触发工具循环。这违背“外层 Agent Planner 拥有工具调度权，上游只做对话表达”。

修正：

- `gateway_context.chat_only_synthesis=true` 时，non-streaming 和 streaming 都跳过工具解析。
- 上游输出的 JSON tool request 作为普通文本返回。
- final synthesis prompt 明确禁止 JSON tool request / function call / tool-use markup。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_final_synthesis_ignores_upstream_json_tool_request \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_fix_loop_upstream_patch_json_is_not_granted_tool_authority -q
# 2 passed
```

意义：chat-only upstream 的工具权限被彻底切断；所有工具步骤必须由 Agent Planner deterministic workflow、downstream client tool execution 或 Gateway-owned service tools 产生。

## 2026-06-26 Observability gap：被忽略的上游伪工具尝试需要进入 runtime timeline

差距：chat-only final synthesis 已经不会执行弱上游输出的 JSON/function-call/tool-use markup；但如果完全静默忽略，远端多租户服务排障时无法知道某个 upstream 模型仍在违背 synthesis-only 约束。

修正：

- non-streaming 与 streaming final synthesis 都调用 `_record_ignored_upstream_tool_attempt()`。
- 只记录 `upstream_tool_attempt_ignored` runtime event。
- event 使用 client 原始 tenant/session/workspace scope。
- event metadata 明确 `tool_authority_granted=false`。
- call payload / response preview 有界，防止弱上游大文本污染 runtime DB。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_final_synthesis_ignores_upstream_json_tool_request \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 2 passed
```

意义：这是远端 Agent Planner Runtime 的安全可观测补丁，不是把工具权限还给模型。Planner 授权工具、Gateway-owned service tool、downstream client tool、ignored upstream attempt 在 runtime timeline 中现在可区分。

## 2026-06-26 Boundary overreach gap：adapter mode 不能被误当成 final synthesis

差距：初版 chat-only hard boundary 过宽，曾把所有 weak/adapter upstream 请求都标记成 `chat_only_synthesis`。这会破坏普通 native/text tool loop：模型第一次返回合法 tool call 时，runtime 会把它当最终文本直接返回。

修正：

- 新增 `_should_use_chat_only_synthesis_boundary()`。
- 只有已经进入 Agent Planner-owned final synthesis 的请求才启用硬边界。
- 普通 orchestration / compatibility tool-loop 继续走原来的 tool extraction 和 execution。

验证：

```bash
python3 -m pytest -q
# 953 passed, 2 skipped, 21 warnings
```

意义：远端服务可以同时承载多类请求：普通 tool loop、Gateway-owned service tool、downstream user-machine tool、Agent Planner final synthesis，不会因为全局 adapter 配置互相污染。

## 2026-06-26 Runtime scope gap：planner sessions 也必须支持 workspace 过滤

差距：远端多租户场景里，Admin Runtime API 已经给 memory/events 传了 `workspace_contains`，但 planner session 查询未接收该过滤条件。结果是同一 tenant 的多个 client workspace 并发时，runtime 聚合视图可能把其他 workspace 的 planner state 混进来。

修正：

- `AgentPlannerStore.list_recent()` 增加 `workspace_contains`。
- `/admin/agent-planner.json` 与 `/admin/agent-runtime.json` planner 查询均传入该参数。
- 测试确认同 tenant / different workspace 的 planner evidence 不会出现在当前 workspace 过滤结果中。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope -q
# 2 passed
```

意义：Agent Planner Runtime 的三类状态面（planner state、memory、events）现在都按 tenant/session/workspace scope 对齐，更符合远端服务的并发隔离要求。

## 2026-06-26 Capability registry gap：Agent 需要可查询的能力/所有权模型

差距：虽然 planner 已经能调度多类工具，但能力边界仍主要隐含在代码里。远端服务中，如果 admin/客户端不知道哪些工具由 Gateway 服务端执行、哪些必须在 client workspace 执行，就仍像一个 gateway adapter，而不是可运维 Agent Runtime。

修正：

- 新增 `planner_capability_catalog()`。
- 新增 `/admin/agent-capabilities.json`。
- `/admin/agent-runtime.json` 返回 `runtime.capabilities`。
- catalog 显式列出 workflows、service-side capabilities、downstream-owned capabilities、HTTP Actions、MCP servers 和 ownership model。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope -q
# 2 passed
```

意义：Agent Planner 的“功能注册 + 意图调度 + 所有权边界”现在成为正式 runtime surface。后续扩展 workflow 或新增 service connector 时，可以先进入 capability registry，再进入 planner intent matching，而不是继续堆散落的 gateway 特判。

## 2026-06-26 Decision history gap：snapshot 需要解释最近决策

差距：Agent Runtime timeline 已经有 `tool_dispatch`，但 planner session snapshot 只显示当前 workflow/step/evidence。如果只看 `/admin/agent-planner.json` 或 final `gateway_context.agent_planner.state`，无法知道最近几次 planner 为什么下发工具、下发了哪些工具。

修正：

- 每次 `_planner_decision()` 都向 state 追加 bounded `decision_history`。
- snapshot 暴露最近 10 条 decision，并提供 `last_decision`。
- persisted state 也带 history，因此 Admin status API 可以直接查看。
- 大参数只保留 preview，避免把工具输入/证据全文塞进 runtime state。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction -q
# passed
```

意义：远端 Agent Planner 现在具备三层可审计性：当前 state、event timeline、decision history。它更接近可运维智能 Agent，而不是一次性的 gateway tool-call shim。

## 2026-06-26 Workflow registry gap：workflow 状态图不能散落在多个模块

差距：planner 已有多个 workflow，但步骤列表同时存在于 `_planner_plan_items()` 和 capability catalog。新增 workflow 时容易忘记同步 Admin/capability surface，导致 Agent Runtime 可观测面和真实调度逻辑漂移。

修正：

- 新增 `WORKFLOW_REGISTRY`。
- 新增 `planner_workflow_catalog()`。
- `_planner_plan_items()` 从 registry 读取 plan items。
- `planner_capability_catalog()` 从 registry 读取 workflows。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_emits_update_plan_before_project_tools_when_declared -q
# 2 passed
```

意义：Agent Planner 的 workflow 图现在成为一等 runtime artifact。后续新增“文档生成”“部署验证”“安全审计”等 workflow 时，可以先注册状态图，再实现 intent routing 和 tool dispatch，而不是继续堆 gateway 特判。

## 2026-06-26 Gap closed：当前意图解析需要成为 runtime artifact

差距：Planner 已经能调度 workflow，但当前 turn 的意图解析没有结构化输出；只能从代码分支或最终 tool_dispatch 倒推。远端服务同时处理多个 tenant/workspace 时，这会让“为什么发起这个工具调用 / 为什么没有调用工具 / 是否被 recalled memory 影响”不可审计。

修正：

- `classify_planner_intent()` 在工具调度前生成结构化 intent。
- intent 字段包括 `kind`、`workflow`、`confidence`、`reason`、`signals`、`source`。
- `planner_state_snapshot()` 暴露当前 intent 和 bounded history。
- runtime timeline 记录 `intent_classification`。
- recalled memory 在 intent parsing 前剥离，防止旧 workspace path 或旧用户目标触发当前工具调度。

剩余后续：当前 classifier 已经是显式 artifact，但部分 routing 仍在 `_generic_intent_decision()` 中保留旧条件分支。后续可以逐步让 routing 直接消费 intent kind/workflow，减少重复判断；但不要把 chat-only upstream 的 JSON/文本伪工具调用升级成工具权限。

## 2026-06-26 Gap closed：intent 需要注册表并参与调度

差距：上一轮已经把 current-turn intent 写入 state/event，但调度函数仍主要重新跑旧条件判断，intent 更像日志字段，不是 Agent Planner 的核心输入；同时远端服务首请求并发可能因为 `_STORE` 无锁懒加载创建多个 SQLite store 实例。

修正：

- 新增 `INTENT_REGISTRY`，让 intent kind 与 workflow、owner、dispatch owner、说明绑定。
- `planner_capability_catalog()` 暴露 intent registry，Admin API 可查询“Planner 能识别哪些意图、这些意图会进入什么 workflow、由谁执行”。
- `_generic_intent_decision()` 优先消费结构化 intent，逐步从 gateway shim 式散落条件转向 Planner intent -> workflow -> dispatch。
- `_STORE_LOCK` 保证远端多用户并发首请求只有一个 `AgentPlannerStore` 初始化者。
- 并发测试覆盖两个用户同时请求不同 workspace，确认 tool dispatch 和 persisted session index 不串。

后续：继续把项目分析 workflow 的 step transition 也抽成状态图/transition table，减少 `plan_downstream_tool_request()` 内部硬编码 step 顺序。

## 2026-06-26 Gap closed：project_analysis 需要 transition table

差距：即使有 workflow registry，`project_analysis` 的真实 step transition 仍散落在 `plan_downstream_tool_request()` 的连续 if 分支中。它可以工作，但扩展到更多 agent workflow 时会继续退化成 gateway 特判。

修正：

- 新增 `PROJECT_ANALYSIS_TRANSITIONS`。
- transition 字段包括 `step`、`condition`、`builder`、`reason`。
- `planner_workflow_catalog()` 暴露 transitions，Admin/capability API 可以看到状态图。
- `_project_analysis_transition_decision()` 根据 transition table 做统一状态转移。
- 主调度函数只负责 intent/session/evidence 准备，然后交给 transition evaluator。

后续：把 `condition` / `builder` 字符串进一步提升为可复用 workflow engine，支持更多 workflow 共享 transition evaluator，例如 docs generation、security review、deploy verification、long-running fix loop。

## 2026-06-26 Gap closed：transition evaluator 需要通用化

差距：`project_analysis` 已经有 transition table，但 evaluator 仍绑定 project-analysis。如果继续这样扩展，`fix_loop`、`test_build`、`code_search` 都会各自长出一套状态机函数，仍不是真正可扩展的 Agent Planner runtime。

修正：

- `_workflow_transition_decision()` 成为通用 transition engine。
- workflow-specific 部分收敛为：context builder、condition handler map、builder handler map。
- `project_analysis` 只是第一个使用者。
- 删除旧 project 专用 builder dispatcher，避免双路径漂移。

后续：把 `fix_loop` / `qa_loop` 的失败诊断、源码 followup、edit 后验证迁入 transition registry，让修复闭环也走同一 workflow engine。

## 2026-06-26 Gap closed：fix_loop / qa_loop 也需要 transition 化

差距：project_analysis 已经进入通用 transition engine，但失败诊断、source followup、Edit 后重新验证仍留在 `_generic_intent_decision()` 的手写分支里。这意味着“分析项目”像 Agent Planner，但“修复/验证”仍像 gateway adapter。

修正：

- `FIX_LOOP_TRANSITIONS` 覆盖 `diagnostic_read` 和 `source_followup_read`。
- `QA_LOOP_TRANSITIONS` 覆盖 `validate_after_test` 和 `validate_after_build`。
- fix/qa workflow 使用同一个 `_workflow_transition_decision()`。
- Runtime capability surface 能看到 fix/qa transitions。
- 新测试证明 source followup transition 会在 diagnostic read 后继续读取 `from src.helper import ...` 推导出的 `src/helper.py`。

后续：继续把 `code_search` 与 `test_build` 的初始工具 dispatch 迁入 transition registry；最终 `_generic_intent_decision()` 应主要只做 intent/workflow 选择和少量显式工具 fallback。

## 2026-06-26 Gap closed：code_search / test_build 初始入口也需要 transition 化

差距：`project_analysis`、`fix_loop`、`qa_loop` 已经进入通用 transition engine，但 `code_search` 和 `test_build` 的初始工具 dispatch 仍直接写在 `_generic_intent_decision()` 中。这会导致常用入口仍像 Gateway adapter 特判，而不是 Agent Planner workflow。

修正：

- `CODE_SEARCH_TRANSITIONS` 注册 `code_search` 初始搜索步骤。
- `TEST_BUILD_TRANSITIONS` 注册 `run_test` / `run_build` 初始验证步骤。
- 两个 workflow 均通过 `_workflow_transition_decision()` 执行，并在 capability catalog 中暴露 transitions。
- `_generic_intent_decision()` 现在优先做 intent/workflow 准备与少量显式工具 fallback，基础 workflow 调度继续向 transition table 收敛。

远端服务边界：这些 transition 只产生 downstream client tool request；不会把 Gateway 服务机 cwd 当用户 workspace，也不会把 chat-only upstream 输出升级成工具权限。

后续：继续把 `generic_tool` 中 read/list/skill/shell/web/custom function fallback 也拆成 transition table；同时保留 Gateway-owned tool 预执行与 downstream-owned user-machine tool 的权限边界。

验证补充：全量 pytest 已通过 `956 passed, 2 skipped`；三个 Agent Planner / project-scope integration smoke 均通过。当前剩余主要是继续把 `generic_tool` fallback 也 transition 化，以及真实高并发/长上下文压测。

## 2026-06-26 Gap closed：generic_tool / edit fallback 也需要 transition 化

差距：即使核心 workflow 已经 transition 化，普通 Skill/Shell/Read/List/Web/custom function 以及 bounded Edit/Write 仍保留在 `_generic_intent_decision()` 中。这会让 Agent Planner 表面上有 workflow registry，但最常用的工具入口仍不可查询、不可审计、难扩展。

修正：

- `GENERIC_TOOL_TRANSITIONS` 公开普通工具入口状态图。
- `EDIT_TRANSITIONS` 公开编辑入口状态图。
- generic/edit dispatch 通过 `_workflow_transition_decision()` 执行。
- `_generic_intent_decision()` 进一步收敛为 intent/workflow coordinator，而不是工具分支堆积点。

当前状态：`project_analysis`、`code_search`、`test_build`、`fix_loop`、`qa_loop`、`generic_tool`、`edit` 均已有公开 transition table。剩余更大工作是把 transition/event/long-context 运行时做压测与更多真实客户端验收，而不是继续补 gateway shim 特判。

验证补充：generic/edit transition 化后，全量 pytest 仍通过 `956 passed, 2 skipped`；三个 Agent Planner / project-scope integration smoke 均通过，证明公开 transition table 没有破坏项目分析、多轮 fail-closed、Claude/Codex client workspace smoke。

## 2026-06-26 Gap closed：Responses 路径 recalled memory 必须注入 input

差距：无限上下文已经支持 Chat/Messages 记忆注入，但 `/v1/responses` 请求的权威字段是 `input`。如果只把 recalled memory 放到 `messages`，当上游协议转换为 OpenAI Chat 时，转换器会忽略该 memory，导致 Responses 用户在长对话后无法稳定拿到周期总结。

修正：

- `/responses` memory injection 改为操作 `input`。
- 新增回归测试证明 recalled memory 进入最终 upstream payload。
- 新增远端压力 smoke：6 个用户/6 个 workspace 并发触发 planner + memory rollup + recall，确认不串 tenant/workspace/session。

意义：无限上下文不再只在 Chat Completions / Messages 上成立，Responses 协议也进入同一 Agent Runtime 记忆闭环。

## 2026-06-26 Gap closed：streaming Responses recall 与 admin 可观测性也要端到端验证

差距：之前已证明非流式 Responses memory injection 和内部 runtime/memory filters，但真正远端 Agent Runtime 还需要证明 streaming 路径和后台 HTTP admin API 都能基于同一套 tenant/workspace/session 范围工作。

修正：

- streaming `/v1/responses` recall 测试覆盖 memory -> upstream payload -> SSE final output。
- 远端压力 smoke 新增真实 admin HTTP 查询，覆盖 `/admin/agent-runtime.json`、`/admin/memories.json`、`/admin/agent-runtime-events.json`。
- 过滤条件包含 tenant、workspace、session、event_type，且检查不会泄漏其他用户 marker。

意义：Agent Planner Runtime 的“无限上下文 + 远端多用户可观测性”不再只靠内部函数验证，而有了端到端 HTTP/admin 证据。

## 2026-07-05 Gap closed：Responses/Codex built-in tool history 也要进入可编程工具链路

差距：OpenAI Responses / Codex 的内建历史项（例如 `local_shell_call`、`tool_search_call` 及其输出）不是普通 `function_call`，之前没有完整进入 Responses↔Chat 转换、Responses SSE tool-call detection、以及 Agent Planner evidence。结果是下游 Codex/Claude Code 看到的历史链路可能丢失工具名、参数或输出证据。

修正：

- 新增 Responses tool history 类型注册，统一识别 `function_call`、`custom_tool_call`、`local_shell_call`、`tool_search_call`、`file_search_call`、`web_search_call`、`computer_call`、`code_interpreter_call`、`mcp_call` 及匹配 output 类型。
- Responses→Chat request/response 转换会把 Codex built-in call 映射成 Chat `assistant.tool_calls`，把 output 映射成 `role=tool`。
- Responses SSE `response.output_item.*` 会检测 Codex built-in tool item。
- Agent Planner evidence 会保留 built-in call 的工具名、`action`/`arguments` 参数和输出内容。
- 云端边界不变：这些 history item 只是协议/证据映射；`local_shell_call` 这类用户 workspace 工具仍属于下游 client，不授权 Gateway 服务机本地执行。

验证：

```bash
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_responses_codex_builtin_tool_history_converts_to_chat_messages \
  tests/test_gateway.py::NativeGatewayTests::test_responses_codex_builtin_tool_response_converts_to_chat_tool_call \
  tests/test_gateway.py::NativeGatewayTests::test_responses_codex_builtin_tool_output_becomes_planner_evidence \
  tests/test_gateway.py::StreamingToolEventTests::test_detect_responses_codex_builtin_tool_call_item
# 4 passed

./scripts/agent_planner_acceptance.sh
# focused gate: 69 passed; PASS

./scripts/agent_planner_acceptance.sh --full
# full pytest: 1045 passed, 2 skipped, 21 warnings; PASS
```
