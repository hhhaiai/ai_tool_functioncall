# 当前逐文件 / 逐类审计与优化记录（2026-07-05）

> 目标来源：`CLAUDE.md` 描述项目总进度，`docs/` 描述具体实现；本文件用当前工作区源码、code graph、AST 清单和本轮测试输出校准真实状态。
> 关键约束：默认上游 **不支持 tool calls / function calls**，不存在其他默认情况；默认路径是 `upstream.tools_enabled=adapter` 且 `supports_tools=false` / `supports_function_calls=false`，由 Gateway Agent Planner / text adapter 补齐协议级工具轮次。

## 1. 总体结论

| 维度 | 当前真实状态 | 证据 / 说明 |
|---|---|---|
| 项目阶段 | 已进入“功能闭环 + 稳定化/边界收敛”阶段 | `CLAUDE.md` 和 `docs/IMPLEMENTATION_STATUS.md` 均把核心能力列为完成；本轮 smoke gate 仍通过。 |
| 默认上游能力 | 默认按弱上游/无原生 tool calls 处理 | 本轮已把配置、Admin UI、runtime fallback、streaming fallback、测试 fixture 全部收敛为 `adapter` 默认。 |
| 下游工具能力 | 下游 Claude Code / Codex 侧工具默认由下游执行；Gateway 只执行 Gateway-owned 工具 | 现有 `gateway_tool_runtime.py` 归属判断、Agent Planner smoke、public-surface smoke 覆盖该边界。 |
| 生产类数量 | `src/` 当前 57 个生产类 | codebase-memory MCP `Class` 查询 + AST 复核。 |
| 本轮已直接优化 | 4 个明确问题 + 默认语义修正 | 并发 active connection、Web2API cache key、curl temp cleanup、权限类别/别名。 |
| 不建议现在大改 | `GatewayHandler`、`AgentPlannerStore`、`gateway_tool_runtime.py` 都较大，但属于核心中枢 | 已有大量测试依赖；无明确失败时不做拆分式重构。 |

## 2. 与旧进度文档的差异

| 文件 | 当前判断 | 需要注意 |
|---|---|---|
| `CLAUDE.md` | 仍可作为功能路线图，但最后更新时间是 2026-06-27，测试数量和部分描述已落后 | 例如默认路径必须明确为无 native tool calls 的 `adapter`，不要再把 `auto` 视作默认。 |
| `docs/IMPLEMENTATION_STATUS.md` | 比 `CLAUDE.md` 更新，已强调 chat-only / weak upstream 适配；但其中全量 pytest 数字是历史快照 | 本轮验证使用了改动相关测试和 `agent_planner_acceptance.sh`。 |
| `docs/CURRENT_AUDIT.md` | 记录 2026-06-19 的安全/拆模块审计，仍有价值 | 但不是本轮 2026-07-05 当前类级审计。 |
| `docs/CLASS_ARCHITECTURE_ANALYSIS.md` | 已过期 | 写着 45+ 类；当前 `src/` 是 57 类，且若干问题（权限、工具归属、加密）已有新实现/新边界。 |

## 3. 生产文件逐个分析

| 文件 | 当前职责 | 到哪一步了 | 处置 |
|---|---|---|---|
| `src/__init__.py` | 包入口、版本/兼容导出 | 可用，轻量 | 保留。 |
| `src/gateway_admin.py` | Admin UI 渲染、客户端配置片段、profiles/skills/MCP 页面 | 可用；本轮已把 Tools 选择器默认改为 `Adapter (default)` | 保留；后续只在 UI 需求明确时改。 |
| `src/gateway_agent_planner.py` | 弱上游场景下的 Agent Planner、intent、evidence、SQLite runtime store | 核心完成；acceptance smoke 覆盖 synthesis/remote pressure/long context | 保留；大文件但不建议无测试拆分。 |
| `src/gateway_app.py` | 旧单体入口兼容、module wrapper、server 启动 | 可用，承担兼容层 | 保留。 |
| `src/gateway_assistants.py` | Gateway-owned assistants/threads 简化处理 | 可用，acceptance gate 覆盖 public surface | 保留。 |
| `src/gateway_builtin_tools.py` | 内置工具 schema、Gateway-owned 工具实现、用户侧工具声明 | 可用，是工具注册中心 | 保留；新增工具需先判定归属。 |
| `src/gateway_cache.py` | SemanticCache、ToolResultCache、embedding provider | 可用；scope-aware 测试覆盖 | 保留；本轮未发现需改。 |
| `src/gateway_claude_compat.py` | Claude Code 工具定义/消息兼容 | 可用 | 保留。 |
| `src/gateway_computer_use.py` | GUI/computer-use 动作适配 | 可用；默认属于用户侧/本地执行边界 | 保留。 |
| `src/gateway_concurrency.py` | 上游连接池、负载均衡、队列、并发请求执行 | 可用；本轮修复 least-connections 计数没有维护的问题 | 已优化并补测试。 |
| `src/gateway_config.py` | 默认配置、配置加载保存、Admin form profile 解析、环境变量映射 | 可用；本轮默认工具模式收敛为 `adapter` | 已优化并补测试。 |
| `src/gateway_context.py` | 无限上下文、压缩、记忆、rollup、fanout | 可用；long-context pressure smoke 覆盖 | 保留；内部函数多但当前稳定。 |
| `src/gateway_encryption.py` | 本地 secret 加密/迁移 | 可用 | 保留。 |
| `src/gateway_errors.py` | 统一错误类型与错误 payload | 可用 | 保留。 |
| `src/gateway_headroom.py` | headroom / 压缩辅助 | 可用，有 `tests/test_headroom.py` | 保留。 |
| `src/gateway_http_actions.py` | HTTP Actions 配置、执行、URL 校验、redirect handler | 可用 | 保留。 |
| `src/gateway_http_handler.py` | HTTP 路由入口、鉴权、Admin/public API、缓存、orchestration 调用 | 可用；非常大但 acceptance gate 覆盖关键 public surface | 保留；只针对明确路由 bug 小改。 |
| `src/gateway_intelligence.py` | 问题分析、质量评估、反思增强 | 可用 | 保留。 |
| `src/gateway_logging.py` | SQLite 日志、请求/失败/工具统计 | 可用 | 保留。 |
| `src/gateway_mcp.py` | MCP session、工具发现、调用、健康状态 | 可用 | 保留。 |
| `src/gateway_permissions.py` | 下游客户端工具权限规则 | 可用；本轮修复类别覆盖不全和 alias/canonical 判断 | 已优化并补测试。 |
| `src/gateway_persistence.py` | SQLite persistence、cache/memory 表迁移 | 可用 | 保留。 |
| `src/gateway_protocol.py` | OpenAI Chat / Responses / Anthropic Messages 协议互转 | 可用；协议严格 smoke 覆盖 | 保留。 |
| `src/gateway_proxy.py` | 上游 native/curl HTTP client、错误映射 | 可用；本轮修复 curl temp payload 异常泄漏 | 已优化并补测试。 |
| `src/gateway_stats.py` | Dashboard/请求/工具/cache/质量/upstream 统计 | 可用 | 保留。 |
| `src/gateway_streaming.py` | SSE streaming、streaming tool event、streaming adapter | 可用；本轮默认工具模式 fallback 收敛为 `adapter` | 已优化。 |
| `src/gateway_tool_runtime.py` | 工具编排核心、tool extraction、归属判断、Agent Planner 接入、直接工具调用 | 核心完成；acceptance gate 覆盖大量弱上游/tool round 场景 | 保留；函数很多但当前不做无证据拆分。 |
| `src/gateway_web2api.py` | 网页抓取、HTML/CSS/regex 结构化提取 | 可用；本轮修复 cache key 未包含输出选项 | 已优化并补测试。 |
| `src/gateway_web_config.py` | Web 配置 schema/render 辅助 | 可用 | 保留。 |
| `src/marketplace.py` | MCP/skills marketplace item 和扫描 | 可用 | 保留。 |
| `src/toolcall_gateway.py` | CLI/script 入口 shim | 可用 | 保留。 |
| `scripts/mock_openai_upstream.py` | 本地 mock upstream | 可用，作为手工/e2e 辅助 | 保留。 |

## 4. 生产类逐个分析

| 类 | 文件 | 判断 | 处理 |
|---|---|---|---|
| `PlannerToolEvidence` | `gateway_agent_planner.py` | planner evidence DTO，必需 | 保留。 |
| `PlannerDecision` | `gateway_agent_planner.py` | planner 决策 DTO，必需 | 保留。 |
| `PlannerIntent` | `gateway_agent_planner.py` | 当前 turn intent，可审计 | 保留。 |
| `AgentPlannerStore` | `gateway_agent_planner.py` | SQLite planner memory / session store，核心 | 保留；不做无证据拆分。 |
| `_GatewayAppModule` | `gateway_app.py` | 旧入口兼容 wrapper | 保留。 |
| `GatewayTool` | `gateway_builtin_tools.py` | 工具 schema 元数据 | 保留。 |
| `ToolCall` | `gateway_builtin_tools.py` | 工具调用 DTO | 保留。 |
| `EmbeddingProvider` | `gateway_cache.py` | embedding provider 抽象 | 保留。 |
| `LocalEmbeddingProvider` | `gateway_cache.py` | 本地 trigram embedding fallback | 保留。 |
| `RemoteEmbeddingProvider` | `gateway_cache.py` | 远程 embedding provider | 保留。 |
| `CacheEntry` | `gateway_cache.py` | semantic cache entry | 保留。 |
| `SemanticCache` | `gateway_cache.py` | scope-aware semantic cache | 保留。 |
| `ToolResultCache` | `gateway_cache.py` | 可缓存工具结果缓存 | 保留。 |
| `ConcurrencyConfig` | `gateway_concurrency.py` | 并发配置 DTO | 保留。 |
| `UpstreamHealth` | `gateway_concurrency.py` | upstream health 状态 | 保留。 |
| `ConnectionPool` | `gateway_concurrency.py` | urllib opener 池 | 保留。 |
| `LoadBalancer` | `gateway_concurrency.py` | upstream 选择/健康/least-connections | 已优化 active connection 计数。 |
| `QueuedRequest` | `gateway_concurrency.py` | 队列请求 DTO | 保留。 |
| `RequestQueue` | `gateway_concurrency.py` | 优先级队列/并发控制 | 保留。 |
| `ConcurrentRequestExecutor` | `gateway_concurrency.py` | 单请求/批量执行 | 已优化 active connection start/end。 |
| `MultiUpstreamManager` | `gateway_concurrency.py` | upstream 管理 facade | 保留。 |
| `GatewayError` | `gateway_errors.py` | gateway 基础异常 | 保留。 |
| `UpstreamHTTPError` | `gateway_errors.py` | 上游 HTTP 错误 | 保留。 |
| `UpstreamTimeoutError` | `gateway_errors.py` | 上游超时错误 | 保留。 |
| `NativeToolVerificationError` | `gateway_errors.py` | native tool probe 错误 | 保留。 |
| `DownstreamAuthError` | `gateway_errors.py` | 下游鉴权错误 | 保留。 |
| `GatewayBusyError` | `gateway_errors.py` | gateway 忙/限流错误 | 保留。 |
| `RequestBodyTooLargeError` | `gateway_errors.py` | body 过大错误 | 保留。 |
| `BadRequestError` | `gateway_errors.py` | bad request 错误 | 保留。 |
| `ConfigError` | `gateway_errors.py` | 配置错误 | 保留。 |
| `ToolExecutionError` | `gateway_errors.py` | 工具执行错误 | 保留。 |
| `ToolResult` | `gateway_errors.py` | 工具执行结果 DTO | 保留。 |
| `_HttpActionRedirectHandler` | `gateway_http_actions.py` | HTTP Action redirect 阻断 handler | 保留。 |
| `GatewayHandler` | `gateway_http_handler.py` | HTTP route handler | 保留；大类但测试覆盖密集，暂不重构。 |
| `IntelligenceConfig` | `gateway_intelligence.py` | 智能增强配置 DTO | 保留。 |
| `QuestionAnalysis` | `gateway_intelligence.py` | 问题分析结果 | 保留。 |
| `QualityAssessment` | `gateway_intelligence.py` | 质量评估结果 | 保留。 |
| `IntelligenceResult` | `gateway_intelligence.py` | 增强处理结果 | 保留。 |
| `_AssessmentResult` | `gateway_intelligence.py` | 兼容 attribute access 的评估结果 | 保留。 |
| `McpSession` | `gateway_mcp.py` | MCP 子进程 session | 保留。 |
| `PermissionRule` | `gateway_permissions.py` | allow/deny pattern rule | 已优化 canonical/alias 匹配。 |
| `ClientPermissions` | `gateway_permissions.py` | 单客户端权限策略 | 已优化类别覆盖。 |
| `PermissionManager` | `gateway_permissions.py` | 全局/客户端权限管理 | 已优化 allowed tools 枚举。 |
| `PersistenceConfig` | `gateway_persistence.py` | persistence 配置 DTO | 保留。 |
| `NativeProxyClient` | `gateway_proxy.py` | 上游 HTTP/curl client | 已优化 temp file cleanup。 |
| `StatsConfig` | `gateway_stats.py` | stats 配置 DTO | 保留。 |
| `RequestStat` | `gateway_stats.py` | 请求统计 DTO | 保留。 |
| `ToolStat` | `gateway_stats.py` | 工具统计 DTO | 保留。 |
| `CacheStat` | `gateway_stats.py` | 缓存统计 DTO | 保留。 |
| `QualityStat` | `gateway_stats.py` | 质量统计 DTO | 保留。 |
| `UpstreamStat` | `gateway_stats.py` | 上游统计 DTO | 保留。 |
| `DashboardData` | `gateway_stats.py` | Dashboard 汇总 DTO | 保留。 |
| `SimpleHTMLExtractor` | `gateway_web2api.py` | HTML parser / simple selector | 保留。 |
| `Web2ApiEngine` | `gateway_web2api.py` | Web2API engine | 已优化 cache key。 |
| `ConfigField` | `gateway_web_config.py` | Web config field schema | 保留。 |
| `ConfigTab` | `gateway_web_config.py` | Web config tab schema | 保留。 |
| `MarketItem` | `marketplace.py` | marketplace item DTO | 保留。 |

## 5. 文档文件当前状态

| 文档 | 当前用途 | 状态 |
|---|---|---|
| `docs/agent-planner-gap-analysis.md` | Agent Planner 缺口分析 | 保留，历史决策参考。 |
| `docs/agent-planner-live-stability-2026-06-27.md` | live stability 记录 | 保留，历史验证参考。 |
| `docs/agent-runtime-architecture.md` | Agent runtime 架构 | 保留。 |
| `docs/agent-runtime-completion-audit.md` | runtime 完成度审计 | 保留。 |
| `docs/agent-runtime-completion-matrix.md` | runtime 完成矩阵 | 保留。 |
| `docs/agent-runtime-requirement-audit.md` | runtime 需求审计 | 保留。 |
| `docs/api-tools-support-product-solution.md` | API tools 产品方案 | 保留。 |
| `docs/ARCHITECTURE.md` | 总体架构 | 保留。 |
| `docs/chat-only-upstream-tool-adapter.md` | 弱上游文本工具适配方案 | 当前核心方案，保留。 |
| `docs/CLASS_ARCHITECTURE_ANALYSIS.md` | 旧类架构分析 | 过期，保留作历史；当前以本文件为准。 |
| `docs/CURRENT_AUDIT.md` | 2026-06-19 审计 | 保留，历史审计。 |
| `docs/DEPLOYMENT.md` | 部署说明 | 保留。 |
| `docs/dialogue-curl-examples.md` | curl 示例 | 保留。 |
| `docs/full-gateway-tool-runtime-marketplace.md` | 完整工具 runtime/marketplace 方案 | 保留。 |
| `docs/gateway-admin-ui-config.md` | Admin UI 配置说明 | 保留。 |
| `docs/gateway-infinite-context-memory.md` | infinite context / memory 说明 | 保留。 |
| `docs/hybrid-gateway-tool-orchestration.md` | hybrid orchestration 方案 | 保留。 |
| `docs/IMPLEMENTATION_STATUS.md` | 当前实现状态主文档 | 保留；建议后续同步本轮默认 adapter 修正。 |
| `docs/native-tool-call-solution.md` | native tool call 方案 | 保留；注意 native 不是默认。 |
| `docs/PERSISTENCE_IMPLEMENTATION.md` | persistence 实现说明 | 保留。 |
| `docs/requirements-and-discussion.md` | 需求讨论 | 保留。 |
| `docs/RUNNING_AND_TESTING.md` | 运行/测试指南 | 已同步默认 adapter 语义。 |
| `docs/tool-failure-analysis-and-enhancements.md` | tool failure 分析 | 保留。 |
| `docs/tool-format-compat-analysis.md` | tool 格式兼容分析 | 保留。 |
| `docs/tool-function-call-shim.md` | function-call shim 方案 | 保留。 |
| `docs/coding-agent-builtin-tools-implementation.md` | coding agent builtin tools 方案 | 保留。 |
| `docs/整理结构.txt` | 结构整理草稿 | 保留，历史草稿。 |
| `docs/progress/*.md` | 进度拆分记录 | 保留，历史进度。 |
| `docs/archive/*.md` | 归档历史 | 不作为当前结论来源。 |

## 6. 本轮优化清单

| 优化点 | 文件 | 回归测试 |
|---|---|---|
| 默认弱上游：`adapter` + capability false | `src/gateway_config.py`, `src/gateway_tool_runtime.py`, `src/gateway_streaming.py`, `src/gateway_admin.py`, `tests/conftest.py`, `tests/test_gateway.py`, `tests/test_config_sync.py`, `README.md`, `docs/RUNNING_AND_TESTING.md` | `tests/test_config_sync.py`, `tests/test_gateway.py -k ...default...` |
| least-connections 真正维护 active connection | `src/gateway_concurrency.py` | `tests/test_concurrency.py` |
| Web2API cache key 包含输出选项 | `src/gateway_web2api.py` | `tests/test_web2api.py` |
| curl payload temp file 异常清理 | `src/gateway_proxy.py` | `tests/test_gateway_proxy_errors.py` |
| 权限类别覆盖 destructive/write 工具并支持 alias canonical 判断 | `src/gateway_permissions.py` | `tests/test_permissions.py` |

## 7. 当前验证

本轮已跑：

```bash
python3 -m pytest -q tests/test_gateway_proxy_errors.py tests/test_config_sync.py tests/test_concurrency.py tests/test_web2api.py tests/test_permissions.py
# 129 passed

python3 -m pytest -q tests/test_gateway.py -k 'native_tools_capable_true_when_explicitly_configured or should_use_text_tool_adapter_defaults_true or upstream_native_tools_capable_defaults_false or upstream_supports_native_tools_defaults_false or chat_only_upstream_config or legacy_passthrough_mode'
# 5 passed

python3 -m pytest -q
# 1053 passed, 2 skipped, 21 warnings

python3 -m compileall -q src tests
# exit 0

git diff --check
# exit 0

./scripts/agent_planner_acceptance.sh
# Agent Planner acceptance gate: PASS；pytest 子集 70 passed
```

## 8. 后续建议（不阻塞当前目标）

1. `GatewayHandler` 和 `gateway_tool_runtime.py` 可以后续按 route/orchestration/provider 拆分，但必须先建立更细的 golden tests；现在直接拆风险大于收益。
2. `docs/CLASS_ARCHITECTURE_ANALYSIS.md` 建议后续归档或替换为本文件，避免“45+ 类”的旧结论误导。
3. 如果继续拆 `GatewayHandler` / `gateway_tool_runtime.py`，建议先补 golden tests，再按 route/orchestration/provider 小步迁移。

## 9. 完整文件清单附录（逐文件处置）

> 说明：本附录覆盖当前 git 跟踪文件，并加入本轮新增审计文档。`src/` 文件的细粒度职责见第 3 节；生产类见第 4 节。

| 文件 | 用途 | 当前处置 |
|---|---|---|
| `.dockerignore` | 容器/部署配置 | 保留。 |
| `.env.example` | 容器/部署配置 | 保留。 |
| `.gitignore` | 项目基础配置/依赖脚本 | 保留。 |
| `CLAUDE.md` | 项目说明/历史报告 | 保留；CLAUDE/README 已同步本轮默认 adapter 结论。 |
| `Dockerfile` | 容器/部署配置 | 保留。 |
| `README.md` | 项目说明/历史报告 | 保留；CLAUDE/README 已同步本轮默认 adapter 结论。 |
| `config/mcp_defaults.json` | 配置模板/运行配置 | 保留；默认弱上游语义已在代码/文档校准。 |
| `docker-compose.prod.yml` | 配置模板/运行配置 | 保留；默认弱上游语义已在代码/文档校准。 |
| `docker-compose.yml` | 配置模板/运行配置 | 保留；默认弱上游语义已在代码/文档校准。 |
| `docs/CURRENT_FILE_CLASS_AUDIT_2026-07-05.md` | 本轮当前逐文件/逐类审计主报告 | 新增，作为当前结论入口。 |
| `docs/ARCHITECTURE.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/CLASS_ARCHITECTURE_ANALYSIS.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/CURRENT_AUDIT.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/DEPLOYMENT.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/IMPLEMENTATION_STATUS.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/PERSISTENCE_IMPLEMENTATION.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/RUNNING_AND_TESTING.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/agent-planner-gap-analysis.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/agent-planner-live-stability-2026-06-27.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/agent-runtime-architecture.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/agent-runtime-completion-audit.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/agent-runtime-completion-matrix.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/agent-runtime-requirement-audit.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/api-tools-support-product-solution.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/archive/CLAUDE_CODE_CONFIG.md` | 归档历史文档 | 保留作历史，不作为当前最终判断来源。 |
| `docs/archive/FINAL_DESIGN_WORKSPACE.md` | 归档历史文档 | 保留作历史，不作为当前最终判断来源。 |
| `docs/archive/FIX_ANALYSIS.md` | 归档历史文档 | 保留作历史，不作为当前最终判断来源。 |
| `docs/archive/FIX_WORKSPACE_ROOT.md` | 归档历史文档 | 保留作历史，不作为当前最终判断来源。 |
| `docs/archive/SECURITY_FIX_WORKSPACE.md` | 归档历史文档 | 保留作历史，不作为当前最终判断来源。 |
| `docs/archive/SUMMARY_WORKSPACE_FIX.md` | 归档历史文档 | 保留作历史，不作为当前最终判断来源。 |
| `docs/chat-only-upstream-tool-adapter.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/coding-agent-builtin-tools-implementation.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/dialogue-curl-examples.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/full-gateway-tool-runtime-marketplace.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/gateway-admin-ui-config.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/gateway-infinite-context-memory.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/hybrid-gateway-tool-orchestration.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/native-tool-call-solution.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/progress/COMPETITIVE_ANALYSIS.md` | 历史进度拆分文档 | 保留作进度资产，当前以本审计和 IMPLEMENTATION_STATUS 为准。 |
| `docs/progress/CRITICAL_FIXES.md` | 历史进度拆分文档 | 保留作进度资产，当前以本审计和 IMPLEMENTATION_STATUS 为准。 |
| `docs/progress/STATUS.md` | 历史进度拆分文档 | 保留作进度资产，当前以本审计和 IMPLEMENTATION_STATUS 为准。 |
| `docs/progress/TODO.md` | 历史进度拆分文档 | 保留作进度资产，当前以本审计和 IMPLEMENTATION_STATUS 为准。 |
| `docs/requirements-and-discussion.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/tool-failure-analysis-and-enhancements.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/tool-format-compat-analysis.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/tool-function-call-shim.md` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `docs/整理结构.txt` | 设计/实现/运行文档 | 保留；当前相关文档已同步或在第 5 节标注。 |
| `examples/chat-with-tool.json` | API 示例 payload | 保留；用于手工调用参考。 |
| `examples/messages-with-tool.json` | API 示例 payload | 保留；用于手工调用参考。 |
| `examples/responses-with-tool.json` | API 示例 payload | 保留；用于手工调用参考。 |
| `gateway.config.json` | 配置模板/运行配置 | 保留；默认弱上游语义已在代码/文档校准。 |
| `gateway.config.yaml` | 配置模板/运行配置 | 保留；默认弱上游语义已在代码/文档校准。 |
| `hermes-skill-deps.sh` | 项目基础配置/依赖脚本 | 保留。 |
| `mcp_defaults.yaml` | 配置模板/运行配置 | 保留；默认弱上游语义已在代码/文档校准。 |
| `nginx/nginx.conf` | 反向代理配置 | 保留。 |
| `requirements.txt` | 项目基础配置/依赖脚本 | 保留。 |
| `scripts/agent_planner_acceptance.sh` | 运维/验证/启动脚本 | 保留；acceptance 脚本已用于本轮验证。 |
| `scripts/claude_m1.sh` | 运维/验证/启动脚本 | 保留；acceptance 脚本已用于本轮验证。 |
| `scripts/deploy.sh` | 运维/验证/启动脚本 | 保留；acceptance 脚本已用于本轮验证。 |
| `scripts/generate-ssl.sh` | 运维/验证/启动脚本 | 保留；acceptance 脚本已用于本轮验证。 |
| `scripts/install_deps.sh` | 运维/验证/启动脚本 | 保留；acceptance 脚本已用于本轮验证。 |
| `scripts/mimo_gateway.sh` | 运维/验证/启动脚本 | 保留；acceptance 脚本已用于本轮验证。 |
| `scripts/mock_openai_upstream.py` | 运维/验证/启动脚本 | 保留；acceptance 脚本已用于本轮验证。 |
| `skills/code-review/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/debug/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/design/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/doc/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/perf/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/plan/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/prompt-eng/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/python-reviewer/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/refactor/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/reflect/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/security/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `skills/test/SKILL.md` | 内置示例 skill | 保留；供 Admin/Skill 功能展示和本地能力测试。 |
| `src/__init__.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_admin.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_agent_planner.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_app.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_assistants.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_builtin_tools.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_cache.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_claude_compat.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_computer_use.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_concurrency.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_config.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_context.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_encryption.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_errors.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_headroom.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_http_actions.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_http_handler.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_intelligence.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_logging.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_mcp.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_permissions.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_persistence.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_protocol.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_proxy.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_stats.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_streaming.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_tool_runtime.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_web2api.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/gateway_web_config.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/marketplace.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `src/toolcall_gateway.py` | 生产源码 | 已在第 3/4 节逐文件/逐类分析；保留或已按证据优化。 |
| `tests/__init__.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/conftest.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/e2e_tool_call_validation.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/integration/agent_planner_long_context_pressure_smoke.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/agent_planner_multiround_smoke.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/agent_planner_project_analysis_smoke.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/agent_planner_protocol_strict_smoke.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/agent_planner_public_surface_smoke.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/agent_planner_remote_pressure_smoke.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/agent_planner_synthesis_guard_smoke.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/project_scope_cli_smoke.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/security_gateway_checks.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/smoke_gateway_tools.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/stress_gateway_concurrency.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/test_gateway_e2e.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/integration/tool_acceptance.py` | 集成 / smoke / 压力测试 | 保留；全量 pytest 已执行，Agent Planner smoke 已执行。 |
| `tests/test_agent_planner_client_context.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_cache_persistence.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_claude_compat.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_concurrency.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_config_sync.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_context_enhanced.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_edge_cases.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_encryption.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_gateway.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_gateway_assistants.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_gateway_proxy_errors.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_headroom.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_intelligence.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_permissions.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_persistence.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_semantic_cache.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_stability.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_stats.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_stats_logging.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_tool_execution_trace.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_tool_parallel.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_web2api.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tests/test_web_config.py` | 单元/回归测试 | 保留；全量 pytest 已执行。 |
| `tool_gateway_audit_report.md` | 项目说明/历史报告 | 保留；CLAUDE/README 已同步本轮默认 adapter 结论。 |
