# 文档中心

> 最后校准：2026-07-24。本文档是仓库内文档的统一入口，用来区分当前事实、专题说明、历史验收记录和工作日志。运行时能力始终以 `GET /capabilities` 和当前代码为准。

## 先读哪些文档

| 目标 | 权威入口 | 说明 |
|---|---|---|
| 了解项目和快速启动 | [`../README.md`](../README.md) | 产品定位、核心能力、最短启动路径、客户端接入和公开端点 |
| 部署、配置和排障 | [`RUNNING_AND_TESTING.md`](RUNNING_AND_TESTING.md) | 本地运行、环境变量、验证命令、测试分层和常见问题 |
| 理解系统边界 | [`ARCHITECTURE.md`](ARCHITECTURE.md) | 上游/Gateway/下游职责、工具归属、请求路径和模块边界 |
| 查看当前实现状态 | [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) | 顶部“当前能力校准”是现行摘要；后续按日期记录历史增量 |
| 生产部署 | [`DEPLOYMENT.md`](DEPLOYMENT.md) | Compose、Nginx、TLS、监控、备份和生产安全约束 |
| 管理面与配置 | [`gateway-admin-ui-config.md`](gateway-admin-ui-config.md) | Control Center、Config Center、下游 Key、MCP、HTTP Actions 和日志 |

## 文档权威性

遇到数字、路径或能力描述冲突时，按以下顺序判断：

1. `GET /capabilities`、`GET /healthz` 和当前源码。
2. 根目录 [`README.md`](../README.md) 与本文档。
3. [`RUNNING_AND_TESTING.md`](RUNNING_AND_TESTING.md)、[`ARCHITECTURE.md`](ARCHITECTURE.md)、[`DEPLOYMENT.md`](DEPLOYMENT.md)。
4. [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) 顶部的最新能力校准。
5. 带日期的审计、验收矩阵、进度记录和 `archive/`。这些文件用于追溯当时证据，不代表当前回归数字或当前公开面。

当前校准事实：

- 公开能力 registry 为 24 条路径；兼容别名会规范化到 canonical 路径。
- Config Center 为 9 个标签页。
- Gateway built-in registry 为 67 个唯一工具、178 个含别名注册项。
- 2026-07-24 全量 pytest 结果为 `1492 passed, 2 skipped`。
- 发布级门禁入口为 `./scripts/ci_gate.sh`；Agent Planner smoke/focused 验收入口为 `./scripts/agent_planner_acceptance.sh`。`--full` 当前存在 strict-mode 环境变量作用域冲突，必须与 clean-env `python3 -m pytest -q` 分开记录，见运行指南。

## 实现真实性矩阵

这里的“已实现”要求至少存在生产入口与真实调用链；只有 helper、stub、schema 或单元测试的能力会明确降级说明。

| 能力 | 生产入口与真实副作用 | 失败路径 | 当前最高证据 | 结论 |
|---|---|---|---|---|
| Chat / Responses / Messages | `gateway_http_handler.py` → canonical orchestration → upstream proxy / 工具归属分流 | 鉴权、ACL、限流/准入、上游错误、工具失败 | public-surface smoke 实际遍历 `/healthz` 广告的 24 条路径 | 已接生产 HTTP |
| Assistants / Threads | 动态资源路由 → Gateway-owned SQLite assistants/threads/messages/runs/steps → canonical chat/tool orchestration | tenant 404、状态冲突、failed、cancel、`requires_action` | HTTP 生命周期、SQLite 重启持久化、跨 tenant 隔离测试 | 已实现；Run 同步，无 Run SSE |
| Web2API | 3 条公开 POST 路径 → 真实 HTTP fetch / HTML extraction | SSRF/DNS/redirect、类型、大小、timeout | 本地真实 HTTP server integration + public-surface smoke | 已接生产 HTTP |
| 多上游 | profile pool → proxy | 非流式 retryable error 跨 profile；400 不切换；熔断/恢复 | 多个本地 upstream 的 failover 测试 | 非流式已实现；SSE 只在已选 profile 内首事件前重试 |
| Intelligence | 非流式/流式 orchestration 的上游请求前分析 → system/reflection prompt enhancement | provider fallback；strict mode 明确失败 | production call-site + provider/单元测试 | 已接请求前链；响应后自动评分/二次反思未接线 |
| Config/Admin API | Basic Auth 路由 → schema-bound update → revision 原子保存 → 定向 runtime reset | same-origin、revision conflict、未知字段、非法值 | live HTTP integration | API 已实现；Config Center 不自动渲染状态 API |
| Rate limit / admission | 所有公开入口 → SQLite token bucket / lease | 429、memory degraded fallback 或 fail closed | 两个真实 Gateway 进程共享状态测试 | 已接所有公开入口 |
| Persistence / maintenance | app 初始化/后台维护 → SQLite cache、retention、bounded cleanup、vacuum | 组件失败进入 metrics/admin 状态 | persistence/maintenance integration | 已实现 |
| Sandbox / process | 工具 runtime → 隔离 worker/process group/session scope | setup fail、timeout、cancel、输出上限 | OS 隔离、子进程清理和多 tenant/workspace 测试 | 已实现 |
| 统计 | Handler/tool runtime → `gateway_logging.py`；Admin dashboard 另读取 `gateway_stats.py` | 日志失败降级；辅助表可为空 | HTTP 统计测试 + 辅助库测试 | 主 HTTP 自动采集真实；辅助 cache/quality/upstream 不是全自动主链 |

### 已知验证与界面边界

- `agent_planner_acceptance.sh --full` 当前把 `GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0` 带入整个 pytest 进程，与两个验证默认值为 `true` 的 config-sync 测试冲突；smoke/focused 可以通过，但该命令现状不能标为全绿。发布时另跑 clean-env 全量 pytest。
- Config Center 当前只编辑 schema/config；状态 API 需要直接调用。页面底部 `/ui/config/client` 与 `/stats` 快捷链接尚未注册，实际入口是 `/client-config` 与 `/api/stats/dashboard`。
- 仓库 mock-upstream/public-surface smoke 能证明 Gateway 路由、协议和副作用。只有配置 `TEST_UPSTREAM_URL` 后运行的 live E2E 才能证明某个外部供应商在当时凭据和网络条件下可用；两类证据不混写。

## 当前操作与参考文档

### 运行、部署与调用

| 文档 | 类型 | 适用范围 |
|---|---|---|
| [`RUNNING_AND_TESTING.md`](RUNNING_AND_TESTING.md) | How-to / Reference | 安装、配置、启动、验证、测试、客户端接入 |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | How-to / Reference | 生产 Compose、Nginx/TLS、安全边界、监控、备份 |
| [`dialogue-curl-examples.md`](dialogue-curl-examples.md) | How-to | Chat、Responses、Messages 与 tools 的 curl 示例 |
| [`chat-only-upstream-tool-adapter.md`](chat-only-upstream-tool-adapter.md) | How-to / Explanation | 弱上游 adapter 的配置、生命周期和验收 |

### 架构与能力专题

| 文档 | 类型 | 适用范围 |
|---|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Explanation / Reference | 当前系统边界与主要模块 |
| [`agent-runtime-architecture.md`](agent-runtime-architecture.md) | Explanation | Agent Planner/Runtime 的详细设计与历史增强记录 |
| [`hybrid-gateway-tool-orchestration.md`](hybrid-gateway-tool-orchestration.md) | Historical design | 早期 native-only 编排方案；当前归属边界仍有参考价值 |
| [`gateway-infinite-context-memory.md`](gateway-infinite-context-memory.md) | Explanation / Reference | 长上下文、SQLite memory、fan-out |
| [`gateway-admin-ui-config.md`](gateway-admin-ui-config.md) | Reference / How-to | Admin 与 Config API、MCP、HTTP Actions |
| [`coding-agent-builtin-tools-implementation.md`](coding-agent-builtin-tools-implementation.md) | Reference | coding-agent 内置工具兼容实现 |
| [`PERSISTENCE_IMPLEMENTATION.md`](PERSISTENCE_IMPLEMENTATION.md) | Explanation | 2026-06-17 SQLite 持久化实现记录 |
| [`tool-format-compat-analysis.md`](tool-format-compat-analysis.md) | Reference | 工具格式兼容映射 |
| [`native-tool-call-solution.md`](native-tool-call-solution.md) | Explanation | 原生工具协议解决方案 |
| [`full-gateway-tool-runtime-marketplace.md`](full-gateway-tool-runtime-marketplace.md) | Design | Tool Runtime 与 Marketplace 方案 |
| [`api-tools-support-product-solution.md`](api-tools-support-product-solution.md) | Design | 产品级能力方案 |
| [`tool-function-call-shim.md`](tool-function-call-shim.md) | Historical design | 早期 shim 方案，现行实现以 adapter/runtime 文档为准 |
| [`tool-failure-analysis-and-enhancements.md`](tool-failure-analysis-and-enhancements.md) | Historical analysis | 早期失败分析与兼容增强 |
| [`requirements-and-discussion.md`](requirements-and-discussion.md) | Historical requirements | 早期需求与讨论记录 |

## 状态、审计与验收记录

以下文件保留原始日期和当时的测试数字。它们是证据快照，不应覆盖本文档顶部的当前校准事实。

| 文档 | 快照日期/用途 |
|---|---|
| [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) | 2026-07-24 顶部为当前摘要，后续为按日期增量日志 |
| [`CURRENT_FILE_CLASS_AUDIT_2026-07-05.md`](CURRENT_FILE_CLASS_AUDIT_2026-07-05.md) | 2026-07-05 逐文件/逐类审计 |
| [`CURRENT_AUDIT.md`](CURRENT_AUDIT.md) | 2026-06-19 结构与安全审计快照 |
| [`CLASS_ARCHITECTURE_ANALYSIS.md`](CLASS_ARCHITECTURE_ANALYSIS.md) | 2026-06-17 类架构快照 |
| [`agent-planner-gap-analysis.md`](agent-planner-gap-analysis.md) | 2026-06-26 Planner 差距关闭过程 |
| [`agent-planner-live-stability-2026-06-27.md`](agent-planner-live-stability-2026-06-27.md) | 2026-06-27 live stability 记录 |
| [`agent-runtime-requirement-audit.md`](agent-runtime-requirement-audit.md) | Agent Runtime requirement 证据 |
| [`agent-runtime-completion-audit.md`](agent-runtime-completion-audit.md) | Agent Runtime completion 证据 |
| [`agent-runtime-completion-matrix.md`](agent-runtime-completion-matrix.md) | 2026-07-07 的 45 项验收矩阵快照 |

仓库根目录的 [`../CLAUDE.md`](../CLAUDE.md)、[`../findings.md`](../findings.md)、[`../progress.md`](../progress.md)、[`../task_plan.md`](../task_plan.md) 和 [`../tool_gateway_audit_report.md`](../tool_gateway_audit_report.md) 是开发过程、调查证据或任务日志，不是面向使用者的当前入口。

## 归档与计划

- `archive/`：Workspace 修复和旧客户端配置材料，包括 [`CLAUDE_CODE_CONFIG.md`](archive/CLAUDE_CODE_CONFIG.md)、[`FINAL_DESIGN_WORKSPACE.md`](archive/FINAL_DESIGN_WORKSPACE.md)、[`FIX_ANALYSIS.md`](archive/FIX_ANALYSIS.md)、[`FIX_WORKSPACE_ROOT.md`](archive/FIX_WORKSPACE_ROOT.md)、[`SECURITY_FIX_WORKSPACE.md`](archive/SECURITY_FIX_WORKSPACE.md) 与 [`SUMMARY_WORKSPACE_FIX.md`](archive/SUMMARY_WORKSPACE_FIX.md)。
- `progress/`：2026-05 的 [`STATUS.md`](progress/STATUS.md)、[`CRITICAL_FIXES.md`](progress/CRITICAL_FIXES.md)、[`COMPETITIVE_ANALYSIS.md`](progress/COMPETITIVE_ANALYSIS.md) 和 [`TODO.md`](progress/TODO.md) 快照；其中 TODO 不代表当前承诺。
- [`superpowers/specs/2026-07-11-strict-cloud-gateway-design.md`](superpowers/specs/2026-07-11-strict-cloud-gateway-design.md)：Strict Cloud Gateway 设计快照。
- [`superpowers/plans/2026-07-11-strict-cloud-gateway.md`](superpowers/plans/2026-07-11-strict-cloud-gateway.md)：对应实施计划。

## 内置示例 Skills

这些文件由 Gateway runtime/管理面加载，不属于项目运维手册，但也纳入文档索引：

[`code-review`](../skills/code-review/SKILL.md) · [`debug`](../skills/debug/SKILL.md) · [`design`](../skills/design/SKILL.md) · [`doc`](../skills/doc/SKILL.md) · [`perf`](../skills/perf/SKILL.md) · [`plan`](../skills/plan/SKILL.md) · [`prompt-eng`](../skills/prompt-eng/SKILL.md) · [`python-reviewer`](../skills/python-reviewer/SKILL.md) · [`refactor`](../skills/refactor/SKILL.md) · [`reflect`](../skills/reflect/SKILL.md) · [`security`](../skills/security/SKILL.md) · [`test`](../skills/test/SKILL.md)

## 最新能力文档覆盖

| 能力 | Reference | How-to | Tutorial | Explanation |
|---|---|---|---|---|
| Chat / Responses / Messages 协议 | README、运行指南 | curl 示例 | 快速开始 | 架构、格式兼容分析 |
| 弱上游工具 adapter 与工具归属 | README、实现状态 | adapter 指南 | 快速开始 | 架构、hybrid orchestration |
| Assistants / Threads 生命周期 | README、运行指南 | 运行指南最小示例 | 快速开始覆盖服务启动 | 架构、实现状态 |
| Web2API | README、运行指南 | curl 示例 | 快速开始覆盖认证 | 架构、实现状态 |
| 多上游非流式故障转移 / SSE 单 profile 边界 | README、运行指南 | Config Center/环境变量 | 无独立教程 | 架构、实现状态 |
| 请求前 Intelligence LLM provider | README、运行指南 | Config Center/环境变量 | 无独立教程 | 架构、实现状态 |
| revision-aware Config API | README、管理面文档 | 运行指南 curl | 无独立教程 | 管理面文档 |
| 生产部署与安全边界 | 部署指南 | Compose/Nginx 步骤 | 生产部署 Quick Start | 架构、部署说明 |

当前没有零覆盖的最新公共能力。多上游、Intelligence 和 Config API 仍可在未来补独立教程，但现有 reference、how-to 和 explanation 足以完成配置与运维。
