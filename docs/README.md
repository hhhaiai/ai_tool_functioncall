# 文档中心

> 最后校准：2026-07-29。所有面向项目的文档统一位于 `docs/`；仓库根目录只保留 [`README.md`](../README.md) 和 [`CLAUDE.md`](../CLAUDE.md) 两个必要入口。

## 权威顺序

遇到能力、路径、数量或运行状态冲突时，按以下顺序判断：

1. `GET /capabilities`、`GET /healthz` 与真实请求/响应；
2. 当前运行配置和部署产物；
3. 当前源码和测试；
4. [`README.md`](../README.md) 与本页；
5. 当前架构、运行、部署和实现状态文档；
6. `archive/` 中的历史审计、设计和进度快照。

历史文档保留的是当时证据、路径和数字，不覆盖当前运行事实。

## 首选入口

| 需求 | 文档 | 类型 |
|---|---|---|
| 了解项目、快速启动、客户端接入、公开端点 | [`../README.md`](../README.md) | Tutorial / Reference |
| 查看当前真实实现、能力边界和验证证据 | [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) | Reference |
| 理解上游、Gateway、下游责任和模块调用链 | [`ARCHITECTURE.md`](ARCHITECTURE.md) | Explanation / Reference |
| 安装、配置、运行、测试和排障 | [`RUNNING_AND_TESTING.md`](RUNNING_AND_TESTING.md) | How-to / Reference |
| Compose、Nginx、TLS、监控和备份 | [`DEPLOYMENT.md`](DEPLOYMENT.md) | How-to / Reference |
| 项目协作和修改门禁 | [`../CLAUDE.md`](../CLAUDE.md) | Contributor Reference |

## 当前运行指南

当前可执行的操作文档集中在 [`guides/`](guides/README.md)：

| 文档 | 用途 |
|---|---|
| [`guides/chat-only-upstream-tool-adapter.md`](guides/chat-only-upstream-tool-adapter.md) | 弱上游 adapter 配置、工具归属和验收 |
| [`guides/dialogue-curl-examples.md`](guides/dialogue-curl-examples.md) | Chat、Responses、Messages 与 tools 的 curl 示例 |
| [`guides/gateway-admin-ui-config.md`](guides/gateway-admin-ui-config.md) | Admin UI、Config API、下游 Key、MCP 和 HTTP Actions |

## 当前技术参考

当前实现说明集中在 [`reference/`](reference/README.md)：

| 文档 | 用途 |
|---|---|
| [`reference/agent-runtime-architecture.md`](reference/agent-runtime-architecture.md) | Agent Planner / Runtime 详细架构 |
| [`reference/coding-agent-builtin-tools-implementation.md`](reference/coding-agent-builtin-tools-implementation.md) | coding-agent 内置工具兼容实现 |
| [`reference/gateway-infinite-context-memory.md`](reference/gateway-infinite-context-memory.md) | 长上下文、SQLite memory 与 fan-out |
| [`reference/tool-format-compat-analysis.md`](reference/tool-format-compat-analysis.md) | OpenAI / Anthropic 工具格式映射 |

## 历史资料

全部历史资料统一位于 [`archive/`](archive/README.md)，按用途分为：

- [`archive/audits/`](archive/README.md#audits)：逐文件审计、完成度矩阵、稳定性记录和持久化实现快照；
- [`archive/designs/`](archive/README.md#designs)：早期产品方案、工具 runtime 方案、shim/native 方案和 Strict Cloud Gateway 计划；
- [`archive/progress/`](archive/README.md#progress)：旧状态、修复清单、竞品分析和 TODO 快照；
- [`archive/workspace/`](archive/README.md#workspace)：旧 Workspace Root 修复和 Claude Code 配置材料。

`findings.md`、`progress.md`、`task_plan.md`、`tool_gateway_audit_report.md` 与 `docs/整理结构.txt` 已删除：它们是过期工作日志或草稿，当前事实已由本页、实现状态、代码和 Git 历史覆盖。

## 当前事实快照

以下数字从当前 registry/schema 生成：

| 项目 | 当前值 |
|---|---:|
| 公开 API 路径 | 24 |
| 唯一内置工具 | 70 |
| 含别名工具 registry key | 199 |
| Config Center 标签页 | 10 |
| Config Center 字段 | 94 |

### 当前发布验证

2026-07-29 的代码与文档整理提交 `45837d91ce8d4294d9b18a979215f9c573f00d7e` 已通过 [GitHub Actions run 30472613525](https://github.com/hhhaiai/ai_tool_functioncall/actions/runs/30472613525)：Python 3.10 与 3.11 均为 `1500 passed, 4 skipped`，静态检查、安全和依赖门禁、Config Center 浏览器回归、development/production Compose 渲染以及 Docker 镜像构建/删除全部成功。四个 skip 是两个实时外部上游用例和两个仅适用于 macOS 的沙箱用例，不代表外部 Provider 已做 live 验证。

### 能力真实性矩阵

| 能力 | 生产入口 | 当前边界 |
|---|---|---|
| Chat / Responses / Messages | `gateway_http_handler.py` → canonical orchestration → proxy / tool runtime | 已接公开 HTTP；外部 Provider 可用性需 live E2E |
| 弱上游工具 adapter | `gateway_tool_runtime.py` / `gateway_agent_planner.py` | Gateway-owned 工具服务端执行；用户侧工具默认下发客户端 |
| Assistants / Threads | 动态资源路由 → `gateway_assistants.py` → SQLite | 支持 messages/runs/steps/tool-output resume；Run 同步，无 Run SSE |
| Web2API | 3 条公开 POST 路径 → `gateway_web2api.py` | 有 SSRF、DNS、redirect、类型、大小和 timeout 边界 |
| 多上游 | `gateway_upstream_pool.py` + `gateway_proxy.py` | 非流式可跨 profile；SSE 不跨 profile |
| Intelligence | 请求发送上游前的分析和 prompt enhancement | 响应后自动评分/二次反思未接主链 |
| Config/Admin | Basic Auth → schema-bound revision update → runtime reset | 10 Tab / 94 字段；per-model 能力使用独立 CAS endpoint；浏览器状态回归必须真实执行 |
| 持久化、限流、准入、沙箱 | SQLite backend / token bucket / lease / worker boundary | 已有多进程、隔离、清理和失败路径测试 |

## 文档维护规则

1. 当前用户文档放在 `docs/`、`docs/guides/` 或 `docs/reference/`。
2. 带日期的审计、旧设计、旧 TODO 和修复过程只放 `docs/archive/`。
3. 不在当前文档中复制大段历史日志；历史通过 Git 和 archive 恢复。
4. 修改路径后必须运行 `tests/test_repository_hygiene.py`，保证本地 Markdown 链接全部可解析、代码围栏闭合。
5. 测试数字只记录完整命令和证据边界；未设置 `TEST_UPSTREAM_URL` 时，不声明外部上游实时通过。
