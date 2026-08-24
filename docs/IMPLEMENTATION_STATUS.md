# Gateway 当前实现状态

> 当前校准日期：2026-07-29。
> 当前事实来源：运行时 registry/schema、生产调用链、自动化测试和发布门禁。
> 历史增量日志已从本文件移除；旧审计、设计和进度记录统一位于 [`archive/`](archive/README.md)。

## 1. 结论

Gateway 的核心闭环已经实现并接入生产 HTTP 路径：

- OpenAI Chat、OpenAI Responses、Anthropic Messages 三协议入口和转换；
- chat-only / weak-tool 上游的文本工具 adapter、Agent Planner、多轮工具编排；
- Gateway-owned 工具、MCP、HTTP Actions、直接工具调用；
- 用户侧工具向 Claude Code / Codex 下发并回填结果；
- 流式 SSE、上下文压缩、SQLite memory、fan-out、缓存；
- Admin UI、10-Tab Config Center、revision-aware 配置更新；
- Assistants / Threads 持久生命周期、Web2API、多上游、请求前 Intelligence；
- 认证、ACL、限流、全局准入、沙箱、日志、统计、维护和部署门禁。

“核心闭环已实现”不等于所有外部环境都无条件可用。以下边界仍然成立：

1. 外部 Provider 的实时兼容性依赖当时的 URL、凭据、模型和网络，必须用 `TEST_UPSTREAM_URL` live E2E 单独证明。
2. Assistants Run 当前是持久同步生命周期，不声明 Run SSE streaming。
3. 多上游跨 profile 故障转移用于非流式请求；SSE 不跨 profile。
4. Intelligence 已接请求前分析和 prompt enhancement；响应后自动评分、二次 LLM 反思和答案重写尚未接主链。
5. 用户机器上的 Read/Write/Edit/Bash/Skill/GUI/local-agent 工具默认由下游客户端执行，不由云端 Gateway 假装执行。

## 2. 运行时规模

以下值由当前代码直接生成：

```text
_supported_public_paths()                  = 24
len(BUILTIN_TOOLS)                         = 199
unique GatewayTool.name                    = 70
len(get_config_schema())                   = 10
sum(len(tab["fields"]) for each tab)      = 94
```

Config Center 的字段分布：

| Tab | 字段数 |
|---|---:|
| upstream | 11 |
| capabilities | 13 |
| context | 11 |
| intelligence | 12 |
| concurrency | 9 |
| cache | 8 |
| tools | 12 |
| web2api | 10 |
| security | 8 |
| model_matrix | 0 |
| **合计** | **94** |

## 3. 公开 API

`_supported_public_paths()` 当前返回 24 条公开路径：

### OpenAI / canonical 前缀

```text
/v1/models
/v1/chat/completions
/v1/chat/completions/count_tokens
/v1/responses
/v1/messages
/v1/messages/count_tokens
/v1/tools/call
/v1/functions/call
/tools/call
/v1/assistants
/v1/threads
/v1/web2api
/api/web2api
```

### Anthropic-compatible 前缀

```text
/anthropic/v1/models
/anthropic/v1/chat/completions
/anthropic/v1/chat/completions/count_tokens
/anthropic/v1/responses
/anthropic/v1/messages
/anthropic/v1/messages/count_tokens
/anthropic/v1/tools/call
/anthropic/v1/functions/call
/anthropic/v1/assistants
/anthropic/v1/threads
/anthropic/v1/web2api
```

兼容别名会先规范化到 canonical 路径。公开路径覆盖由 Agent Planner public-surface smoke 实际遍历，不仅检查常量存在。

## 4. 能力矩阵

| 能力 | 生产调用链 | 已验证行为 | 当前边界 | 状态 |
|---|---|---|---|---|
| Chat / Responses / Messages | `gateway_http_handler.py` → canonical orchestration → protocol/proxy/tool runtime | 三协议请求、响应、错误和工具格式转换 | 外部 Provider 需 live E2E | 已实现 |
| 下游认证与 ACL | Handler → downstream key → path ACL | Bearer/x-api-key、租户隔离、管理面 Basic Auth | 默认凭据仅适合本地开发 | 已实现 |
| 弱上游 adapter | Handler → Agent Planner/tool runtime → upstream synthesis | 文本工具调用解析、多轮执行、结果回填、final synthesis | 质量受上游模型遵循度影响 | 已实现 |
| 工具归属分流 | Tool runtime → Gateway-owned executor 或 downstream native request | MCP/HTTP/网络/纯函数/记忆服务端执行；用户工具下发 | 服务端用户工具执行必须显式授权 | 已实现 |
| 直接工具接口 | `/v1/tools/call`, `/v1/functions/call`, `/tools/call` | Gateway-owned 工具不依赖上游模型可直接执行 | 工具权限和输入 schema 仍生效 | 已实现 |
| 内置工具 registry | `gateway_builtin_tools.py` | 70 canonical / 199 registry keys；权限、workspace 和 output limit | 部分工具依赖本机程序或显式开关 | 已实现 |
| Per-model 能力路由 | `gateway_model_router.py` + `gateway_config.py` + `gateway_proxy.py` | 13 项可配置能力标志、稀疏 per-model 覆盖、共享状态、健康过滤与 failover/round_robin/random/first/least_connections | recognition 严格选路；普通 tools/function/parallel/web-search/stream/json-schema/network/vision/audio/speech 请求在 request boundary 推导完整能力组合并优先选匹配 model；text-tool adapter 只替代 tool 协议能力，无可用匹配时保留既有 Gateway adapter/profile 路径 | 已接入请求主链；真实 Provider 仍需部署后验证 |
| 识图 / 识音乐 / 识视频 | `gateway_builtin_tools.py` → `call_upstream_llm` → ModelRouter → upstream | 图片适配 Chat/Responses/Anthropic；音乐仅 OpenAI Chat base64/local MP3/WAV；视频无标准 adapter 时返回 `unsupported_media_transport`，不伪装成图片 | 需上游真实支持；URL/本地/base64 有归属、MIME、大小和私网边界 | 部分实现，fail-closed |
| MCP | `gateway_mcp.py` | servers、tools/list、tools/call、public name 映射 | 外部 MCP 可用性取决于其进程/网络 | 已实现 |
| HTTP Actions | `gateway_http_actions.py` | schema、启用状态、执行、Admin 管理 | 受 SSRF/网络和超时边界限制 | 已实现 |
| Streaming | `gateway_streaming.py` | passthrough、增量 safe text、工具决策轮、断连取消 | chat-only rewrite 可为正确性缓冲；SSE 不跨 profile | 已实现 |
| Context / memory | `gateway_context.py` | token 估算、压缩、SQLite recall、项目隔离、fan-out | “无限上下文”是有界压缩/分片效果 | 已实现 |
| Semantic cache | `gateway_cache.py` | 精确指纹、相似匹配、持久化、清理 | 非流式、非工具请求优先 | 已实现 |
| Assistants / Threads | `gateway_assistants.py` | assistants/threads/messages/runs/steps、tool-output resume、tenant 隔离、重启持久化 | Run 同步，无 Run SSE | 已实现 |
| Web2API | `gateway_web2api.py` | 真实 HTTP fetch、HTML extraction、3 个公开别名 | SSRF/DNS/redirect/type/size/timeout 限制 | 已实现 |
| 多上游 | `gateway_upstream_pool.py` + `gateway_proxy.py` | 选择、熔断、恢复、retryable error failover | 非流式跨 profile；SSE 单 profile | 已实现（有明确边界） |
| Intelligence | orchestration 请求前 → `gateway_intelligence.py` / `gateway_llm.py` | 规则/LLM 分析、system/reflection prompt enhancement、provider fallback/strict failure | 响应后自动评分与二次反思未接线 | 部分实现，已接请求前主链 |
| Config Center | `/ui/config` + `/api/config*` | 10 Tab、94 字段、revision、schema-bound update、per-model capability CAS、secret placeholder、runtime reset | 需要 Basic Auth 和 same-origin 写保护 | 已实现 |
| Admin/状态 | `gateway_admin.py` / `gateway_admin_api.py` | 配置、stats、cache、upstream、intelligence、skills、MCP、HTTP Actions | 状态卡区分正常、降级和加载失败 | 已实现 |
| Rate limit / admission | 所有公开入口 → SQLite/memory backend | token bucket、lease、多进程共享、degraded/fail-closed | 由配置决定 backend 和失败策略 | 已实现 |
| Sandbox/process | Tool runtime → sandbox worker/process group | workspace、timeout、cancel、输出上限、子进程清理 | OS 能力依赖部署环境 | 已实现 |
| Logging/stats | Handler/runtime → `gateway_logging.py`; dashboard 辅助读取 `gateway_stats.py` | 请求、失败、工具、metrics、trace ring、敏感字段遮盖 | 辅助 Q&A 表不是所有主请求的唯一事实源 | 已实现 |
| Deployment | Dockerfile、两套 Compose、Nginx、CI gate | Compose render、Docker build/remove、配置检查 | 生产凭据/TLS/外部存储由部署环境提供 | 已实现 |

## 5. 工具执行模型

### Gateway-owned

典型类别：

- 纯函数和计算；
- HTTP Actions；
- MCP tools；
- 网络/抓取类工具（受私网和 SSRF 边界约束）；
- Gateway memory / recall；
- 直接公开工具端点。

这些工具在 Gateway runtime 中真实执行，结果回填上游或直接返回下游。

### Downstream-owned

典型类别：

- Read / LS / Glob / Grep；
- Write / Edit / ApplyPatch；
- Bash / shell / code interpreter；
- Skill / plugin；
- GUI / computer-use；
- local agent / user workspace 操作。

默认行为是返回下游原生 `tool_use` / `tool_calls` / `function_call`，由 Claude Code 或 Codex 在用户机器执行，再把结果送回 Gateway。项目根从请求体和客户端上下文解析；Gateway 服务启动目录不能冒充用户项目目录。

## 6. 配置和安全边界

已接入的关键边界包括：

- 下游 Bearer/x-api-key 认证和 path ACL；
- Admin Basic Auth；
- Config API same-origin 写保护和 revision 乐观并发；
- secret placeholder `***` 不覆盖已有密钥；
- 配置损坏 fail closed，不回退到默认无鉴权状态；
- CORS allowlist，无 wildcard credential 模式；
- SSRF、DNS rebinding、redirect、response size、timeout 限制；
- SQLite tenant/workspace 隔离；
- 沙箱、进程组、取消、输出上限；
- 日志和配置的敏感字段递归遮盖；
- `.gateway_service.json`、runtime DB、trace、`.env*`、证书和密钥均被 Git 忽略。

生产部署必须替换开发默认 Admin 凭据，并由部署环境重新运行门禁。

## 7. 测试证据

### 当前整理发布验证

2026-07-29，代码与文档整理提交 `45837d91ce8d4294d9b18a979215f9c573f00d7e` 已由 [GitHub Actions run 30472613525](https://github.com/hhhaiai/ai_tool_functioncall/actions/runs/30472613525) 在两套 Python 环境完整验证：

```text
Python 3.10:                success, 1500 passed, 4 skipped
Python 3.11:                success, 1500 passed, 4 skipped
Compile/config/Ruff/Mypy:   PASS
Bandit/pip check/pip-audit: PASS（无已知依赖漏洞）
Compose development/prod:  PASS
Docker build/remove:       PASS
```

CI 运行在 Linux；四个 skip 分别是两个未设置 `TEST_UPSTREAM_URL` 的实时外部上游用例，以及两个仅在 macOS 执行的 `sandbox-exec` 强制隔离用例。Config Center 的 Playwright/Chromium 浏览器测试没有被跳过。

### Agent Planner 完整验收基线

2026-07-28，commit `0c2bfbdb3a0b973a5a377fb63dfbf528ea3c02b5`：

```text
Agent Planner focused gate: 92 passed
Full pytest:                1495 passed, 2 skipped
Agent Planner gate:         PASS
CI gate:                    PASS
GitHub Python 3.10:         success
GitHub Python 3.11:         success
GitHub run:                 30290530328
```

两个 skip 是未设置 `TEST_UPSTREAM_URL` 的外部上游用例，因此该验收证明 Gateway 本身、mock/public-surface 集成、浏览器配置中心和部署门禁，不证明任意外部 Provider 在 2026-07-28 之后仍实时可用。

### 当前发布门禁

```bash
unset GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN
python3 -m pytest -ra tests
./scripts/agent_planner_acceptance.sh --full
./scripts/ci_gate.sh
```

`scripts/ci_gate.sh` 包含：

- `compileall`；
- Ruff；
- Mypy；
- Bandit；
- `pip check` / `pip-audit`；
- 全量 pytest；
- 配置、Git 和 secret guard；
- `docker-compose.yml` 与 `docker-compose.prod.yml` 渲染；
- Docker 镜像构建和删除。

GitHub CI 在 Python 3.10 和 3.11 上执行同一门禁，并安装 Playwright Chromium；`GATEWAY_REQUIRE_BROWSER_TESTS=1` 禁止 Config Center 浏览器回归静默跳过。

## 8. 当前未声明为完整的事项

| 事项 | 当前结论 | 完成它需要什么 |
|---|---|---|
| 任意外部 Provider 永久兼容 | 不声明 | 针对具体 Provider、模型、URL、凭据运行 live E2E |
| Assistants Run SSE | 未实现/未声明 | 设计并实现 run event streaming、断连和恢复语义 |
| SSE 跨上游 profile failover | 未实现/未声明 | 首事件前跨 profile 选择、幂等和防重放设计 |
| 响应后 Intelligence 自动重写 | helper 存在，主链未接 | 评分、二次 LLM、预算、失败回退和可观测性接线 |
| 云端默认执行用户机器工具 | 明确不做 | 这是所有权/安全边界，不是缺少实现 |

## 9. 文档和代码整洁性

2026-07-29 的整理规则：

- 根目录只保留 `README.md` 和 `CLAUDE.md` 两个 Markdown 入口；
- 当前文档集中在 `docs/`、`docs/guides/`、`docs/reference/`；
- 历史审计、设计、进度和 Workspace 修复材料集中在 `docs/archive/`；
- 删除无引用的根目录工作日志、草稿、`config/mcp_defaults.json` 和项目外 Hermes 安装脚本；
- `src/gateway_admin.py` 只保留一个真实 `_render_admin_ui()`，删除不可达 legacy renderer、空 loader 和无用 import；
- `tests/test_repository_hygiene.py` 防止退休文件、重复 renderer、断链和未闭合代码围栏回流。

详细导航见 [`README.md`](README.md)。
