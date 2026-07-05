# Gateway 实现状态文档

> 最后更新: 2026-07-05

## 2026-06-26 Chat-only 上游原生工具协议适配状态

当前已完成对真实上游 `http://47.85.40.209:8885` 的弱工具能力适配。该上游普通对话、Responses、Messages 和 stream 可用，但 native tools/function calls 实测不可用；Gateway 现在以 `adapter` 模式在外层合成真实协议级工具轮次，并让 Codex / Claude Code 在用户机器执行本地工具。

关键状态：
- `.gateway_service.json`：本地 gitignored 运行配置，包含真实上游地址与加密后的上游密钥。
- `gateway.config.json`：无密钥模板，声明该上游为 `supports_tools=false` / `supports_function_calls=false` / `supports_streaming=true`。
- `src/gateway_tool_runtime.py`：负责项目分析、读文件、Skill、自定义函数、web search 等意图到协议级工具调用的合成与 schema 适配。
- `src/gateway_http_handler.py`：禁止缓存 tool request 回合，避免语义缓存复用过期工具调用。
- `tests/integration/project_scope_cli_smoke.py`：可用 chat-only mock 上游验证 Claude Code / Codex CLI 双回合工具执行。
- 配置后台入口：主入口为 `/ui`；兼容旧入口 `/config`、`/admin`、`/admin/config-ui`，均受 Basic Auth 保护并渲染 Gateway Control Center。
- 项目分析工具策略：对 Claude Code 的 `分析这套项目` 优先触发 `Skill(codebase-onboarding)`；后续弱上游 prose fallback 会使用已声明的 `Bash`，避免下发客户端未声明的 `LS/Glob` 或错误参数。

验证结果：
```bash
python3 -m pytest -q
# 896 passed, 2 skipped, 21 warnings

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass: true
```

具体方案见：[`docs/chat-only-upstream-tool-adapter.md`](chat-only-upstream-tool-adapter.md)。

---

## 2026-06-19 收敛状态

本轮按最终 Gateway 目标做全仓库回归：上游可为不支持 tools/function calls 的普通 API；下游面向 Claude Code / Codex；Gateway 负责协议适配、必要的文本工具适配、workspace 隔离、记忆/上下文治理、gateway-owned 工具执行，以及用户侧工具的协议级下发。

关键收敛点：
- 普通无 tools 请求不再自动注入大段工具 adapter，避免污染上游普通对话。
- 请求级 workspace 不再默认回退 Gateway 服务 cwd；缺失 workspace 时使用匿名隔离空间，显式 env/config root 仅作兜底。
- 工具归属已明确：HTTP Action/MCP/WebFetch/WebSearch/image_generation/calculator/Memory 等 gateway-owned 工具由 Gateway 真执行；Read/LS/Glob/Grep/Write/Edit/Bash/Skill/computer_use/click/type/press/scroll/local agent 等用户机器工具默认返回下游原生 tool request，由 Claude Code/Codex 在用户机器执行。
- 弱上游文本 `<function=...>`、强制 `tool_choice`、local planner 和 streaming adapter 都统一走同一归属判断；用户侧工具不再因为 Gateway 有内置实现就落到服务机执行。
- 本机 mock/upstream/Web2API 直连绕过 macOS 系统代理，测试与本地部署不再受 `127.0.0.1:1082` 等代理影响。
- 配置文件保留稳定 hash，明文密码仍归一化；Admin 无 workspace 时可正常渲染全局 skills。

验证结果：
```bash
python3 -m compileall -q src tests
python3 -m pytest -q
# 886 passed, 2 skipped

local mock smoke
# OK: healthz, models, chat, direct calculator, user-side LS delegation
```

---

## 架构概览

```
下游 (Codex/Claude Code/DeepSeek/OpenCode)
    ↓ HTTP
Gateway Handler (gateway_http_handler.py)
    ├─ 智力增强 (gateway_intelligence.py)  ← 请求预处理
    ├─ 语义缓存 (gateway_cache.py)         ← 缓存命中加速
    ├─ 统计记录 (gateway_stats.py)         ← Q&A 数据积累
    ├─ 协议转换 (gateway_protocol.py)      ← Anthropic ↔ OpenAI ↔ Responses
    ├─ 工具编排 (gateway_tool_runtime.py)  ← gateway-owned 执行 / 用户侧工具下发
    ├─ 流式处理 (gateway_streaming.py)     ← SSE 流式响应
    ├─ 上下文管理 (gateway_context.py)     ← 无限上下文/记忆/扇出
    └─ 并发优化 (gateway_concurrency.py)   ← 连接池/负载均衡
    ↓ HTTP
上游 API (OpenAI/Anthropic/自定义)
```

## 已完成并集成功能

### 1. 无限上下文 (Infinite Context) ✅

**实现模块**: `src/gateway_context.py`
**集成位置**: `run_tool_orchestration` / `run_streaming_orchestration`

核心功能:
- Token 估算算法 (ASCII 4字符/token, CJK 2字符/token)
- 消息压缩 (保留最近N条 + LLM摘要旧消息)
- 文本分块 (按 token 数量分块处理长文本)
- 扇出并行 (长对话分块并行处理，结果合成)
- 记忆系统 (SQLite 持久化，关键词提取，会话隔离)
- 线程安全 Summary 缓存 (LRU 淘汰, sha256 哈希)

配置项:
```json
{
  "context": {
    "enabled": true,
    "max_input_tokens": 24000,
    "keep_recent_messages": 12,
    "summary_max_chars": 6000,
    "fanout_enabled": true,
    "fanout_chunk_tokens": 12000,
    "memory_enabled": true
  }
}
```

---

### 2. Tool Calls / Function Calls ✅

**实现模块**:
- `src/gateway_tool_runtime.py` - 工具执行引擎
- `src/gateway_builtin_tools.py` - 内置工具定义
- `src/gateway_claude_compat.py` - Claude Code 兼容层

**集成位置**: `run_tool_orchestration` 内的工具编排循环

支持的协议:
- OpenAI Chat Completions (`/v1/chat/completions`)
- OpenAI Responses (`/v1/responses`)
- Anthropic Messages (`/v1/messages`)

工具归属:
- Gateway-owned（Gateway 真执行）: `echo_probe`, `calculator`, `get_current_time`, HTTP Actions（如天气/内部 API）, MCP tools/resources, `WebFetch`, `WebSearch`, `image_generation`, `Memory`, 纯函数/状态类工具。
- User-side（默认下发给 Claude Code/Codex 执行）: `Read`, `ReadManyFiles`, `FileInfo`, `LS`, `Tree`, `Glob`, `Grep`, `Write`, `Edit`, `Bash`, `Git`, `PythonSymbols`, `Skill`, `computer_use`/`click`/`type_text`/`press_key`/`scroll` 等依赖用户机器/项目/桌面的工具。
- 兼容旧本地代理部署：只有显式设置 `gateway.execute_user_side_tools_in_gateway=true` 后，才允许用户侧工具在 Gateway 服务机执行；`delegate_tools_to_downstream=false` 不再授权云端本地执行用户 workspace 工具。

Claude Code 兼容:
- 支持 `input_schema` (Anthropic) ↔ `parameters` (OpenAI) 格式互转
- 支持 `mcp__server__tool` 命名格式
- 支持 37+ Claude Code 工具定义 (Agent, Bash, Read, Write, Edit, Glob, Grep 等)

Gateway 本地执行策略（仅 gateway-owned/显式 opt-in 本地工具）:
```
读工具 (Read, Glob, Grep, WebFetch) → 并行执行
写工具 (Write, Edit, Bash) → 串行执行
```

---

### 3. 语义缓存 (Semantic Caching) ✅

**实现模块**: `src/gateway_cache.py`
**集成位置**: HTTP Handler 层 (非流式请求)

组件:
- `SemanticCache` - 基于嵌入向量的语义缓存
- `ToolResultCache` - 工具结果缓存 (确定性工具)
- `LocalEmbeddingProvider` - 本地嵌入向量 (字符 trigram + 词频)
- `RemoteEmbeddingProvider` - 远程嵌入服务

缓存策略:
- 精确匹配优先 (sha256 哈希)
- 语义相似度兜底 (余弦相似度)
- 相似度阈值: 0.92 (可配置)
- TTL: 3600 秒 (可配置)
- 最大条目: 1000 (可配置)
- Session-aware 缓存键 (防止跨会话缓存泄露)

配置项:
```json
{
  "cache": {
    "enabled": true,
    "max_entries": 1000,
    "similarity_threshold": 0.92,
    "ttl_seconds": 3600
  }
}
```

---

### 4. Web2API ✅

**实现模块**: `src/gateway_web2api.py`
**集成位置**: HTTP Handler `/api/web2api` 路由

功能:
- `Web2ApiEngine` - Web 页面转 API 引擎
- `SimpleHTMLExtractor` - HTML 解析器
- CSS 选择器提取 (tag, .class, #id, tag.class, tag#id)
- 正则表达式提取
- 元数据提取 (title, meta, og:*)
- 自动提取模式
- SSRF 防护 (拒绝私有/回环地址)

API:
```
POST /api/web2api
{
  "url": "https://example.com",
  "selectors": {"title": "h1", "content": "article p"},
  "mode": "css|regex|auto"
}
```

---

### 5. 协议转换 ✅

**实现模块**: `src/gateway_protocol.py`

支持的转换:
- OpenAI Chat ↔ Anthropic Messages
- OpenAI Chat ↔ OpenAI Responses
- Anthropic Messages ↔ OpenAI Responses
- 工具格式互转 (function_call ↔ tool_use ↔ input_schema)
- 多模态图片转换 (Anthropic base64/url → OpenAI image_url)
- System prompt 多段拼接
- Reasoning/thinking 独立返回

---

### 6. 流式处理 ✅

**实现模块**: `src/gateway_streaming.py`

功能:
- SSE (Server-Sent Events) 流式响应
- 流式工具调用检测 + 执行 + 结果回传
- 流式协议转换 (Anthropic ↔ OpenAI ↔ Responses)
- 流式语义缓存（请求前检查 + 响应后存储）
- 流式工具执行 + 结果回传

---

### 7. 智力提升 (Intelligence Enhancement) ✅

**实现模块**: `src/gateway_intelligence.py`
**集成位置**: HTTP Handler 层 (非流式请求预处理)

核心功能:
- 问题分析 (`_analyze_question`)
- 复杂度检测 (语义信号评分: simple/moderate/complex)
- 领域识别 (code/math/general/creative/factual)
- 问题分解 (`_decompose_question`)
- 反思机制 (`_generate_reflection`)
- 回答质量评估 (`_assess_answer_quality`)
- 增强系统提示构建
- 自动注入到请求 body 中

配置项:
```json
{
  "intelligence": {
    "enabled": true,
    "reflection_enabled": true,
    "decomposition_enabled": true,
    "quality_assessment_enabled": true,
    "quality_threshold": 0.6
  }
}
```

---

### 8. Web 配置界面 (Admin UI) ✅

**实现模块**: `src/gateway_web_config.py`
**集成位置**: HTTP Handler `/ui/config` 路由

核心功能:
- Tab 式配置界面 (9 个配置标签页)
- 上游配置 (URL, API Key, 模型, 超时)
- 能力配置 (tools, function_calls, streaming, vision)
- 上下文配置 (无限上下文参数)
- 智力提升配置 (反思、分解、质量评估)
- 并发配置 (连接池、负载均衡策略)
- 缓存配置 (语义缓存、工具缓存)
- 工具配置 (内置工具、Claude 兼容、MCP、HTTP Actions)
- Web2API 配置
- 安全配置 (认证、限流、CORS)

API:
```
GET  /ui/config           → 配置 UI 页面
GET  /api/config/schema    → 配置 Schema JSON
POST /api/config/update    → 更新配置
```

---

### 9. 问答统计 (Q&A Statistics) ✅

**实现模块**: `src/gateway_stats.py`
**集成位置**: HTTP Handler 层 (每次请求记录)

核心功能:
- 请求统计 (成功率、响应时间、token 使用)
- 工具调用统计 (使用频率、失败率、执行时间)
- 缓存统计 (命中率、相似度)
- 质量统计 (完整性、相关性、清晰度、准确性)
- 上游统计 (各上游成功率、响应时间)
- 综合仪表板 (`get_dashboard`)
- 趋势分析 (`get_hourly_trends`)
- Top 查询分析 (`get_top_paths`, `get_top_tools`)
- CSV 导出功能

API:
```
GET /api/stats/dashboard   → 统计仪表板
GET /api/cache/stats       → 缓存统计
GET /api/cache/clear       → 清除缓存
```

---

### 10. 并发优化 (Concurrency Optimization) ✅

**实现模块**: `src/gateway_concurrency.py` + `src/gateway_proxy.py`

集成状态:
- `NativeProxyClient` 使用 opener 连接复用（减少 TCP 握手开销）
- 自动重试 502/503/504 错误（30秒间隔，最长20分钟）
- 可配置超时时间

独立功能（按需启用）:
- `ConnectionPool` - HTTP 连接池
- `LoadBalancer` - 负载均衡（round_robin/least_connections/random）
- `MultiUpstreamManager` - 多上游管理 + 健康检查
- `ConcurrentRequestExecutor` - 并发请求执行

配置项:
```json
{
  "concurrency": {
    "enabled": true,
    "max_connections": 100,
    "max_connections_per_host": 10,
    "retry_count": 2,
    "load_balance_strategy": "round_robin"
  }
}
```

---

## 安全特性

- 请求大小限制 (max_request_body_bytes)
- 跨域写入拦截 (admin origin check)
- 凭证泄露防护 (config redaction)
- Config fail-closed (无效配置拒绝启动)
- Auth 前置 (请求 body 读取前验证)
- SSRF 防护 (Web2API/WebFetch 拒绝私有地址)
- Constant-time 密码比较 (hmac.compare_digest)
- API Key 哈希存储 (sha256)

---

## 部署说明

### 环境变量
```bash
# 上游 API 配置
GATEWAY_UPSTREAM_URL=https://api.example.com
GATEWAY_UPSTREAM_KEY=sk-xxx
GATEWAY_UPSTREAM_PROTOCOL=openai_chat

# 服务配置
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8885

# 缓存配置
GATEWAY_CACHE_ENABLED=true
GATEWAY_CACHE_TTL_SECONDS=3600

# 智力提升
GATEWAY_INTELLIGENCE_ENABLED=true

# 上下文配置
GATEWAY_CONTEXT_ENABLED=true
GATEWAY_CONTEXT_MAX_INPUT_TOKENS=24000

# 下游认证
GATEWAY_DOWNSTREAM_KEY=your-api-key

# 管理员认证
GATEWAY_ADMIN_PASSWORD=your-admin-password
```

### 启动命令
```bash
# 开发环境
python3 -m src.gateway_app --host 127.0.0.1 --port 8885

# 生产环境 (多 worker)
gunicorn src.gateway_app:app -w 4 -b 0.0.0.0:8885
```

---

## 测试覆盖

| 模块 | 测试文件 | 状态 |
|------|----------|------|
| gateway_protocol | test_gateway.py | ✅ |
| gateway_context | test_gateway.py, test_context_enhanced.py | ✅ |
| gateway_tool_runtime | test_gateway.py, test_tool_parallel.py | ✅ |
| gateway_streaming | test_gateway.py | ✅ |
| gateway_cache | test_semantic_cache.py | ✅ |
| gateway_intelligence | test_intelligence.py | ✅ |
| gateway_web2api | test_web2api.py | ✅ |
| gateway_web_config | test_web_config.py | ✅ |
| gateway_stats | test_stats.py, test_stats_logging.py | ✅ |
| gateway_concurrency | test_concurrency.py | ✅ |
| gateway_claude_compat | test_claude_compat.py | ✅ |
| 边界条件 | test_edge_cases.py | ✅ |
| 稳定性 | test_stability.py | ✅ |
| 集成测试 | test_gateway_e2e.py | ✅ |
| **总计** | **886 passed, 2 skipped** | ✅ |

---

## 后续迭代

### Phase 2.5 (当前必须补齐): Agent Planner

用户已用原生支持 tool 的 API 对比 `分析这套项目`，确认当前 gateway adapter 与原生工具 Agent 差距很大。
因此 “Tool Calls 完整支持” 需要降级解释为：协议转换、工具请求表面化、部分弱上游兜底已实现；**完整外层 Agent Planner 尚未完成**。

当前已完成的校正：

- `分析这套项目` 在 `Skill` 声明且 `codebase-onboarding` 可用时，会优先返回 `Skill(codebase-onboarding)`。
- 修复弱上游文本 fallback 中 `files to analyze` 被误识别为 `LS(path="to")` 的问题。
- 当 `LS/Glob` 未声明但 `Bash` 声明时，项目分析 fallback 使用声明过的 shell 工具。
- `/config` 已作为管理 UI 别名可访问。

仍缺的核心能力：

- planner state：已新增 `.gateway_runtime/agent_planner.sqlite3` 基础状态存储，仍需扩展更多 workflow；
- workflow planner：已覆盖项目分析主路径 `Skill -> project structure -> key file read -> synthesis`，并迁入显式 Skill/shell/read/list/web/custom-function/code_search/test-build/edit/write/fix-loop 诊断读取、结构化 patch->Edit、Edit/Write 后自动验证；仍需真实客户端多轮执行验证；
- evidence compaction：已实现周期性 LLM summary 优先、rolling extractive fallback；
- final synthesis prompt：已在非流式路径注入 planner evidence，chat-only upstream 不再负责猜工具；
- streaming parity：已接入 direct planner/evidence injection，仍需覆盖更多 streaming 多轮验收；
- 完整验收：`分析这套项目` 已具备多轮规划骨架，但还需真实 Claude Code/Codex 长链路 smoke。

设计文档：[agent-planner-gap-analysis.md](agent-planner-gap-analysis.md)

### 2026-06-26 实现增量: gateway_agent_planner.py

新增模块：`src/gateway_agent_planner.py`

职责：

- 在 chat-only upstream 之前做 intent/workflow planning；
- 只选择下游声明过的工具，避免虚构 `LS(path="to")` 这类错误；
- 为项目分析和通用工具意图维护 planner state、step 和 evidence summary；
- 周期性压缩工具结果：默认每 4 个新 tool result 或 summary 超阈值触发 LLM 摘要，失败后保留 rolling extractive fallback；
- 推断 codebase-memory MCP 的 `project` 参数：优先 `GATEWAY_CODEBASE_MEMORY_PROJECT`，否则从 workspace root 生成项目名；
- 对 `运行测试` / `build` / `typecheck` 生成下游执行的自动 runner shell 命令；
- 对明确的编辑/写入请求生成 `Edit` / `Write` 下游工具调用；
- 对失败的测试/构建结果抽取 traceback 文件并生成诊断 `Read`；
- diagnostic `Read` 后，上游若返回严格 JSON `Edit` 请求，gateway 会重新适配为下游声明 schema 后返回给客户端执行；
- `Edit` / `Write` 成功结果回传后，如果任务包含测试/修复意图，会自动下发验证命令形成 QA repeat loop；
- 验证通过后会停止工具循环，注入 pass evidence 交给 chat-only upstream 生成最终总结；
- 让上游模型只做最终对话/总结，而不是决定工具调用。

已接入：

- `src/gateway_tool_runtime.py::_direct_downstream_tool_request_response`
- `src/gateway_tool_runtime.py::_run_tool_orchestration_scoped`
- `src/gateway_streaming.py::_run_streaming_orchestration_scoped`

已验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

### Phase 3 (未来增强)
- [ ] 多租户支持
- [ ] API 限流增强
- [ ] 监控告警集成
- [ ] 分布式部署支持
- [ ] 流式请求缓存集成
- [ ] 并发模块集成到 upstream 转发

---

## 安全与敏感信息

**以下文件包含真实 API 地址/密钥，已加入 `.gitignore`，不会被提交：**

| 文件 | 内容 | gitignore |
|------|------|-----------|
| `.gateway_service.json` | 上游 API 地址、密钥、下游 key | ✅ |
| `.case.txt` | 测试用 curl 命令（含真实 IP） | ✅ |
| `.gateway_runtime/` | 运行时配置缓存 | ✅ |
| `.traces/` | Claude Code 调用 trace | ✅ |
| `.env` | 环境变量（密钥） | ✅ |

**验证命令：**
```bash
git ls-files | xargs grep -l '47\.85\.40\.209' 2>/dev/null
# 应无输出
```

### 2026-06-26 增量: multi-round fix_loop smoke 已闭环

新增/修正能力：

- `fix_loop` 不再满足于“失败输出 -> 直接让弱上游猜补丁”。
- 失败 evidence 中的第三方 runtime 路径会被过滤，避免读取 `site-packages` / Python framework warning 路径。
- `Read` / `Bash` / `Edit` 等 tool_use 的输入参数会写入 planner evidence，保留证据来源。
- 对带行号的 Python 读取结果，planner 会解析 `from src.app import ...` / `import src.app`，继续读取对应源码 `src/app.py`。
- 本地自包含 smoke 证明了完整链路：
  1. planner 下发测试命令；
  2. 测试失败后读取失败测试；
  3. 从测试 import 继续读取被测源码；
  4. evidence 完整后才调用 chat-only upstream 产出结构化 `Edit`；
  5. 执行 Edit 后自动 rerun pytest；
  6. pass evidence 注入 upstream 做最终 synthesis。

验证命令：

```bash
python3 tests/integration/agent_planner_multiround_smoke.py
# {"ok": true, "steps": ["Bash", "Read", "Read", "Read", "Read", "Edit", "Bash"], "upstream_calls": 2}

python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py src/gateway_http_handler.py tests/integration/agent_planner_multiround_smoke.py

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

剩余风险：该 smoke 使用 fake chat-only upstream 和本地临时 workspace；还需要真实 Claude Code/Codex 客户端、多轮 streaming、真实上游长上下文 evidence compaction 压测。

### 2026-06-26 增量: Planner session 与 context compaction 顺序修复

问题：之前 planner evidence 的顺序是“注入 evidence -> 全局 context compaction”。当 chat-only upstream 上下文窗口较小时，`_maybe_compact_request_for_upstream()` 可能压缩/替换 system prompt，导致 Agent Planner 辛苦收集的 evidence 被传输层压没。

修复：

- `planner_session_key()` 改为锚定首个真实用户请求，工具结果回传不再改变 session key。
- 支持 metadata / JSON-string metadata 中的 `conversation_id`、`thread_id`、`session_id`。
- non-streaming 与 streaming orchestration 都改为：
  1. `prepare_upstream_body(path, memory_body)` 先持久化完整 evidence；
  2. `_maybe_compact_request_for_upstream()` 压缩实际发送 payload；
  3. `prepare_upstream_body(path, compacted_body)` 再注入 compact planner summary。
- 这样“无限上下文”变成两层：全局消息压缩负责控制 payload；planner SQLite evidence summary 负责保留任务证据和工具链路。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_stays_stable_across_tool_result_turns \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction -q
# 2 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

### 2026-06-26 增量: planner_progress / update_plan 调度

为了更接近原生 coding agent，Agent Planner 新增显式计划工具调度：

- 对项目分析、测试修复、代码搜索、编辑等多步任务，如果下游声明了 `update_plan` 或 `TodoWrite`，首轮先返回计划工具调用。
- 计划工具回传后，workflow 继续进入 `Skill(codebase-onboarding)` / 项目结构收集 / 诊断读取等真实工具链路。
- 如果下游没有声明计划工具，不会生成未知工具调用，保持旧客户端兼容。

这将 planner 从“工具选择器”继续推进为“有计划状态的外层 agent”。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_emits_update_plan_before_project_tools_when_declared \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_to_skill_after_update_plan_result -q
# 2 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

### 2026-06-26 增量: project_analysis codebase-memory 优先级

项目分析 workflow 的结构收集阶段新增 codebase-memory/MCP 优先级：

```text
get_architecture -> search_graph -> search_code -> LS/Glob -> Bash fallback
```

要点：

- 如果下游声明了 `mcp__codebase_memory_mcp__search_graph`，planner 会优先用它收集项目架构/入口/路由/配置/测试相关图谱证据。
- `search_graph/search_code` 自动补 `project` 参数。
- 只有缺少 code graph/MCP 工具时才回落到 LS/Glob/Bash。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_codebase_search_graph_for_project_structure -q
# 1 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests   tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream   tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool   tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool   tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

### 2026-06-26 增量: streaming planner evidence / context compaction parity

修复并验证 streaming orchestration 的 planner evidence 注入链路：

- `_run_streaming_orchestration_scoped()` 现在在自身作用域 import `prepare_upstream_body`，避免非 direct-response 路径 NameError。
- 新增测试覆盖：streaming 请求携带 tool_result 且触发 context compaction 时，上游请求仍包含 `Gateway Agent Planner evidence` 和关键文件证据。
- SSE 输出正常结束，包含最终文本和 `message_stop`，无 `event: error`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction -q
# 1 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

## 2026-06-26 Agent Planner core-flow trace 增量

本轮继续按“外层 Agent Planner”目标推进：`project_analysis` 在 planner-managed `project_structure` 证据返回后，会追加 `core_flow_trace`，优先使用 codebase-memory `search_graph/search_code` 追入口、路由、handler、tool execution 等核心流程，再进入关键文件读取与最终综合。

关键代码：
- `src/gateway_agent_planner.py`
  - 新增 planner call id step 解析：`planner_<step>_<uuid>` -> `completed_steps`。
  - 新增 `core_flow_trace` 工具调度：`search_graph` -> `search_code` -> Bash grep fallback。
- `tests/test_gateway.py`
  - 新增 core-flow trace 回归。
  - 新增 completed step 记录回归。

验证：
```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 34 passed

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true
```

## 2026-06-26 Agent Planner symbol deep-dive 增量

`project_analysis` workflow 新增符号级深挖：当 `core_flow_trace` 的 code graph evidence 包含 `qualified_name` 时，planner 会进入 `symbol_deep_dive`，优先请求 `get_code_snippet` 和 `trace_path`，为 chat-only upstream 提供源码与调用链证据。

关键代码：
- `src/gateway_agent_planner.py`
  - `QUALIFIED_NAME_RE`
  - `_qualified_names_from_evidence()`
  - `_symbol_deep_dive_calls()`
  - `plan_downstream_tool_request()` 中 `core_flow_trace -> symbol_deep_dive` 状态迁移。
- `tests/test_gateway.py`
  - `test_agent_planner_deep_dives_symbol_after_core_flow_trace`

验证：
```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

## 2026-06-26 Streaming 多轮 Agent Planner 验收

新增 streaming 多轮 planner 回归，证明 stream 模式下外层 planner 能跨请求继续调度：

```text
Skill(codebase-onboarding)
  -> search_graph(project architecture)
  -> search_graph(core request flow)
```

新增测试：
- `tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools`

验证：
```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools -q
# 1 passed

python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction -q
# 5 passed
```

## 2026-06-26 Streaming symbol deep-dive 最终综合验收

新增 streaming final synthesis 回归：当 `project_structure/core_flow_trace/symbol_deep_dive` 工具结果都已回传，streaming orchestration 会把完整 evidence 写入 Agent Planner，再压缩 payload 并重新注入 `Gateway Agent Planner evidence`，最终只调用 chat-only upstream 做最终表达。

新增测试：
- `tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive`

验证：
```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 1 passed

python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction -q
# 3 passed
```

## 2026-06-26 真实 Claude/Codex CLI smoke 修复

运行真实 CLI smoke：

```bash
python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
```

先后发现并修复：

- smoke 脚本自身未设置 `NO_PROXY/no_proxy`，可能把本地 `127.0.0.1` 请求发到系统代理。
- Codex Responses 下 one-shot read 工具会重复调度，已在 `src/gateway_agent_planner.py::_generic_intent_decision` 中对已有 evidence 的 Skill/shell/read/list 请求停止重复下发。
- Responses SSE completed usage 缺少 `total_tokens`，已在 `src/gateway_streaming.py::_stream_final_response` 中归一化。

最终验证：
```bash
python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true
# claude.ok=true
# codex.ok=true

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_responses_has_item_before_text_delta \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 39 passed
```

## 2026-06-26 完整 project_analysis 长链路 smoke

新增集成 smoke：

- `tests/integration/agent_planner_project_analysis_smoke.py`

覆盖完整项目分析链路：

```text
update_plan -> Skill -> search_graph(project) -> search_graph(core flow)
  -> get_code_snippet -> trace_path -> Read -> final synthesis
```

同时修复：

- `src/gateway_agent_planner.py`：planner session anchor 跳过 `[Gateway recalled memory]` 与 planner evidence 注入文本，避免 memory recall 后 state key 漂移。
- `src/gateway_protocol.py`：Anthropic messages 转 OpenAI Chat 时合并多个 system 消息，避免 planner evidence system prompt 被原始 system 覆盖。

验证：
```bash
python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_responses_has_item_before_text_delta \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 39 passed
```

## 2026-06-26 回归收口：Agent Planner 边界修复与全量通过

本轮根据真实 tool API 对比继续校正外层 Agent Planner，修复两个导致“看起来差别很大”的关键边界：

- `src/gateway_agent_planner.py`
  - 新增 `_gateway_owned_tool_name()` 懒加载识别 HTTP Action / MCP tool。
  - `_custom_function_tool_call()` 不再把 Gateway-owned 工具当 caller-private custom function 下发给客户端。
  - `plan_downstream_tool_request()` 的 `project_analysis` 只在请求声明了下游工具时运行；无 tools 的普通 chat 请求继续走 upstream/context fanout，不生成未声明 synthetic tool。

修复原因：

1. **HTTP Action round-trip**：Gateway-owned 工具必须由 Gateway 执行并把 tool result 追加给 upstream；planner 抢先 surface 给 downstream 会直接破坏 round-trip。
2. **Forced fanout**：无 tools 请求没有可执行的 client-side tool surface；project_analysis planner 抢先发 `Glob/LS` 会绕过原有 too-long forced fanout。

验证结果：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::NativeGatewayTests::test_upstream_too_long_response_triggers_forced_fanout -q
# 2 passed

python3 -m pytest -q
# 923 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true

git diff --check
# clean

grep -RIn --exclude-dir=.git --exclude-dir=.gateway_runtime --exclude='.gateway_service.json' --exclude='.case.txt' '<redacted-real-key>' . 2>/dev/null || true
# clean
```

状态：全量测试已通过；Agent Planner 现在保留三类边界：downstream user-machine tools、Gateway-owned tools、plain no-tools upstream/context path。

## 2026-06-26 无限上下文增强：summary + recent 覆盖 Chat/Messages/Responses

本轮继续推进“Gateway -> 外层 Agent Planner”的目标，补齐普通历史压缩路径，避免只靠截断造成长会话失忆。

修改：

- `src/gateway_context.py::_compact_messages_with_summary()`
  - LLM 摘要失败时生成 role-labelled extractive digest。
  - 旧消息被压缩为 bounded summary，recent messages 原样保留。
- `src/gateway_context.py::_compact_request_for_upstream()`
  - Chat Completions：system 注入 gateway compaction prompt + previous summary。
  - Anthropic Messages：summary 合并到 `system` 字段，不生成非法 `role=system` message；原始 system 会按 `summary_max_chars` 裁剪。
  - Responses：`input` list 也进入 summary + recent 压缩。
- `tests/test_gateway.py`
  - 新增 Chat/Messages/Responses 三个 context compaction 回归。

验证：

```bash
python3 -m pytest tests/test_gateway.py::ContextSummarizationTests \
  tests/test_gateway.py::NativeGatewayTests::test_text_tool_adapter_compacts_huge_claude_code_payload_before_upstream -q
# 8 passed

python3 -m pytest tests/test_gateway.py::ContextSummarizationTests \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_upstream_too_long_response_triggers_forced_fanout -q
# 10 passed

python3 -m pytest -q
# 926 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

状态：planner evidence 与普通会话历史现在都有周期/窗口压缩路径。该能力让 chat-only upstream 在上下文有限时继续接收历史摘要和最新证据，而不是让 Gateway 退化为单轮 proxy。

## 2026-06-26 Gateway-owned tool preexecute planner

目标推进：减少 chat-only upstream 的工具选择职责，让 Gateway/Agent Planner 先执行 Gateway-owned service tools，再把结果交给上游做最终语言表达。

修改：

- `src/gateway_tool_runtime.py`
  - `_gateway_owned_tool_call_from_user_text()`：对请求声明工具做 intent scoring；匹配 HTTP Action / MCP connector 时推断参数。
  - `_preexecute_gateway_owned_planner_tool()`：执行 Gateway-owned tool，使用 `_append_tool_results()` 把 tool result 加入上下文。
  - `_run_tool_orchestration_scoped()`：weak upstream 模式下在 upstream final synthesis 前运行 preexecute。
  - preexecute 后移除 `tools/tool_choice`，防止 text adapter 继续要求 chat-only upstream 输出工具调用格式。
- `tests/test_gateway.py`
  - `test_gateway_owned_weather_http_action_executes_and_roundtrips` 改为验证 planner preexecute：HTTP server 先收到 `city=Shanghai`，upstream 只调用一次且请求中包含 `temp_c` tool result，没有 `tools/tool_choice`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_exposes_schema_and_executes_real_http \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_http_error_records_tool_failure \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_response_max_bytes_is_enforced -q
# 4 passed

python3 -m pytest -q
# 926 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

状态：Gateway-owned HTTP Action 已进入 planner-owned execution path。旧的 upstream-text-tool path 仍作为兜底存在，但明显匹配的 service-side 工具不再依赖上游发明 XML/function-call。

## 2026-06-26 Streaming Gateway-owned preexecute 对齐

本轮补齐 streaming 路径：上轮 non-streaming 已支持 planner 在 upstream 前执行 Gateway-owned HTTP/MCP tool，但 streaming 仍缺这一层。

修改：

- `src/gateway_streaming.py`
  - `_run_streaming_orchestration_scoped()` 导入并调用 `_preexecute_gateway_owned_planner_tool()`。
  - weak upstream 模式下，direct downstream/local 响应之后、planner evidence/context compaction/upstream synthesis 之前，先执行 Gateway-owned tool。
- `tests/test_gateway.py`
  - 新增 `test_streaming_gateway_owned_http_action_preexecutes_before_upstream`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction -q
# 4 passed

python3 -m pytest -q
# 927 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

状态：Gateway-owned planner preexecute 已覆盖 sync + stream。后续继续扩展更多 service-side/MCP 多步 workflow 时，应同时补两条路径。

## 2026-06-26 Service-side HTTP Action registry for planner

目标推进：让外层 Agent Planner 使用 Gateway 自己配置的服务侧能力，而不是只依赖客户端每次请求显式传入 `tools`。

修改：

- `src/gateway_tool_runtime.py`
  - `_gateway_owned_tool_call_from_user_text()` 现在合并：
    1. caller-declared Gateway-owned tools；
    2. `_enabled_http_actions()` 中的 configured HTTP Actions。
  - 当用户意图与配置 action 匹配时，planner 推断参数并预执行。
- `tests/test_gateway.py`
  - non-streaming weather HTTP Action 回归移除 request `tools`，验证从配置注册表发现 action。
  - streaming weather HTTP Action 回归同样移除 request `tools`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream -q
# 2 passed

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_upstream_too_long_response_triggers_forced_fanout \
  tests/test_gateway.py::ToolCallDefaultTests::test_text_tool_adapter_keeps_plain_chat_plain_without_tool_intent \
  tests/test_gateway.py::ToolCallDefaultTests::test_text_tool_adapter_strips_tools_and_injects_prompt -q
# 3 passed

python3 -m pytest -q
# 927 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

状态：configured HTTP Actions 已是 planner-discoverable service-side capability。plain chat 与 forced fanout 回归仍通过，说明该 registry 不会泛化污染普通请求。

## 2026-06-26 Configured MCP service-side registry for planner

目标推进：HTTP Action registry 已支持无 request tools 声明预执行，本轮把 configured MCP connector 纳入同一外层 Agent Planner 能力注册表。

修改：

- `src/gateway_tool_runtime.py`
  - import `_enabled_mcp_servers`, `_mcp_list_server_tools`, `_mcp_public_name`。
  - `_gateway_owned_tool_call_from_user_text()` 现在合并：
    1. caller-declared Gateway-owned tools；
    2. configured HTTP Actions；
    3. configured MCP server tools。
  - MCP discovery best-effort；单个 server/list 失败不阻塞普通请求。
- `tests/test_gateway.py`
  - 新增真实 stdio MCP sync preexecute 回归。
  - 新增 streaming MCP preexecute 回归。
  - 新增 cache 清理，避免新增 preexecute 测试污染既有 MCP catalog cache 断言。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_mcp_stdio_tools_list_call_and_schema_merge \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_configured_mcp_tool_preexecutes_without_request_tools -q
# 3 passed

python3 -m pytest -q
# 929 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

状态：service-side registry 已覆盖 HTTP Action + MCP。Agent Planner 已能从 Gateway 配置发现服务侧工具并在 upstream 前执行；这进一步降低了 chat-only 模型参与工具选择/调用格式生成的需求。

## 2026-06-26 Planner observability metadata propagation

目标推进：真实 Agent Runtime 不仅要能调工具，还要能解释“这一轮为什么调了工具、走了哪个 workflow”。之前 `gateway_context.agent_planner` 只存在于发给 upstream 的内部 synthesis request；最终响应可能像普通模型回复，排查 tool/preexecute 行为不直观。

修改：

- `src/gateway_tool_runtime.py`
  - 新增/使用 `_attach_request_gateway_context(response, request_body)`。
  - 在无后续 tool calls 的 final synthesis 分支，把 planner/runtime metadata 附到最终响应。
- `src/gateway_streaming.py`
  - streaming final synthesis 分支也调用同一 helper。
  - 修复 scoped streaming import 遗漏 `_attach_request_gateway_context` 导致的 SSE `name '_attach_request_gateway_context' is not defined`。
- `tests/test_gateway.py`
  - sync HTTP Action preexecute 与 configured MCP preexecute 均断言最终响应含 `gateway_context.agent_planner.workflow == gateway_owned_tool`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools -q
# 2 passed

python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_configured_mcp_tool_preexecutes_without_request_tools -q
# 2 passed
```

状态：non-streaming final response 已可观测 planner workflow；streaming 路径已不再因 metadata helper 缺失报错。后续若要在 SSE chunk 中逐事件暴露 planner metadata，需要单独定义兼容 OpenAI/Anthropic stream 的调试事件格式。

## 2026-06-26 Built-in service capability registry

目标推进：Agent Planner 的能力注册表不应只来自客户端 request tools 或外部配置。Gateway 自带的纯工具/服务工具也应属于 runtime 可调度能力；chat-only upstream 不应负责生成 calculator/time/search 的 tool-call 格式。

修改：

- `src/gateway_tool_runtime.py`
  - `_gateway_owned_tool_call_from_user_text()` 增加 built-in capability discovery。
  - 当前纳入：`calculator`、`current_time`、`WebSearch`。
  - 风险边界：只有非 user-machine 工具进入 service-side preexecute；本地文件、写入、shell、GUI、local agent 仍由下游执行。
  - 计算/时间/搜索意图使用 planner 直接选择工具并推断参数。
- `tests/test_gateway.py`
  - `test_gateway_owned_builtin_calculator_preexecutes_without_request_tools`
  - `test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream`

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_builtin_calculator_preexecutes_without_request_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream -q
# 4 passed

python3 -m pytest tests/test_gateway.py::ToolCallDefaultTests::test_text_tool_adapter_keeps_plain_chat_plain_without_tool_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_web_search_uses_declared_downstream_tool_name \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_repeat_read_after_tool_result \
  tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_configured_mcp_tool_preexecutes_without_request_tools -q
# 5 passed
```

状态：service-side registry 已扩展为 configured HTTP Action + configured MCP + selected built-in Gateway tools。普通 plain chat 与 declared downstream web-search 回归保持通过。

## 2026-06-26 Conversation Memory periodic rollup

目标推进：用户要求“上游如果上下文有限，希望实现无限上下文，隔一段时间总结一下”。已有 over-limit compaction 和 per-turn memory，但缺少不依赖超限触发的周期总结。本轮加入会话级 periodic rollup。

修改：

- `src/gateway_context.py`
  - `_maybe_rollup_conversation_memory()`：每轮写入 memory 后检查是否达到 `memory_rollup_every_turns`。
  - `_memory_build_rollup_summary()`：构建 `[Periodic conversation summary]`；LLM summary 可选，默认 extractive fallback。
  - `_sqlite_latest_rollup()` / `_sqlite_recent_memories_since_rollup()`：读取最新 rollup 与自上次 rollup 后的普通 memories。
  - `_prepend_latest_rollup()`：召回 memory 时把最新 session rollup 前置。
- `src/gateway_config.py` / `gateway.config.json`
  - `memory_rollup_every_turns` 默认 8。
  - `memory_rollup_max_chars` 默认 4000。
- `tests/test_gateway.py`
  - `test_conversation_memory_periodic_rollup_is_recalled` 覆盖 rollup 写入与后续召回注入。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_recalls_same_session_workspace_only \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_compacts_huge_turns_in_sqlite -q
# 3 passed
```

状态：长期上下文现在有三层：1) request over-limit summary；2) per-turn SQLite memories；3) periodic session rollup。chat-only upstream 不再承担记住全部历史的职责。

## 2026-06-26 Remote service boundary and multi-tenant isolation

用户强调：这不是本地增强服务，而是远端 Agent Planner 服务。目录必须是 client 自己的 workspace，多用户同时请求必须稳定工作，不能污染服务机 workspace 或跨用户共享状态。

修改：

- `src/gateway_tool_runtime.py`
  - `_create_anonymous_workspace()` 不再按 prompt hash 分配匿名空间。
  - 无身份请求使用随机 per-request anonymous workspace。
  - 有身份请求使用 `tenant/user + session` 的 hash 目录名，避免 path traversal 和 session id 撞车。
- `src/gateway_agent_planner.py`
  - `planner_session_key()` 增加 tenant/user 维度。
  - `AgentPlannerStore` 使用线程锁、SQLite `busy_timeout=30000`、WAL 初始化，降低并发写入锁冲突。
- `src/gateway_context.py`
  - `_memory_session_key()` 增加 tenant/user 维度。
  - 匿名无 session 请求不再从 prompt 派生稳定 memory session，避免跨用户记忆污染。
- `tests/test_gateway.py`
  - `test_remote_anonymous_workspace_is_not_shared_by_identical_prompts`
  - `test_remote_anonymous_workspace_is_tenant_session_scoped`
  - `test_agent_planner_session_key_is_tenant_scoped_for_remote_service`
  - `test_parallel_direct_tool_calls_keep_client_workspaces_isolated`

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_parallel_direct_tool_calls_keep_client_workspaces_isolated \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_not_shared_by_identical_prompts \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_tenant_session_scoped \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_is_tenant_scoped_for_remote_service -q
# 4 passed
```

状态：远端服务边界更清晰。Agent Planner 状态、conversation memory、anonymous workspace 都已按 tenant/session/workspace 隔离；client workspace 通过 request scope / ContextVar 保持线程隔离。

## 2026-06-26 Remote runtime state isolation hardening

目标：按远端多租户 Agent Planner 服务审计，而不是按本地增强服务假设审计。重点补齐进程级 runtime state 的裸 id 碰撞风险。

修改：

- `src/gateway_builtin_tools.py`
  - 增加 `_RUNTIME_SCOPE_OVERRIDE`、`_runtime_scope_key()`、`_scoped_runtime_id()`。
  - `EXEC_SESSIONS`、`AGENT_SESSIONS`、`TEAM_SESSIONS`、`PENDING_USER_QUESTIONS` 内部 key 改为 `runtime_scope + public_id`。
  - 对外协议仍暴露调用方原始 id，避免破坏客户端兼容。
  - `SendMessage` 避免在 `AGENT_SESSIONS_LOCK` 持锁状态下调用 `_tool_send_input()`，消除潜在自锁。
- `src/gateway_tool_runtime.py`
  - `_workspace_scope(root, body)` 同时设置 workspace root 和 runtime scope。
  - runtime scope 包含 tenant/user、session/conversation、resolved client workspace。
  - 匿名无 session 请求使用随机 request scope，不能靠 prompt 或裸 id 共享状态。
- `src/gateway_agent_planner.py`
  - 去掉 `Path.cwd()` fallback，避免服务机 cwd 参与 planner key / codebase project 推断。
- `gateway.config.json` / `gateway.config.yaml`
  - 移除默认 `gateway.workspace_root=./workspace`。远端缺少 client workspace 时应进入 anonymous isolated workspace，而不是服务机目录。
- `tests/test_gateway.py`
  - 新增 exec shell session 同名跨用户/跨 workspace 隔离测试。
  - 新增 team mailbox 同名跨用户/跨 workspace 隔离测试。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_not_shared_by_identical_prompts \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_tenant_session_scoped \
  tests/test_gateway.py::NativeGatewayTests::test_parallel_direct_tool_calls_keep_client_workspaces_isolated \
  tests/test_gateway.py::NativeGatewayTests::test_remote_exec_sessions_are_scoped_by_client_workspace_and_tenant \
  tests/test_gateway.py::NativeGatewayTests::test_remote_team_mailboxes_are_scoped_by_client_workspace_and_tenant \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_is_tenant_scoped_for_remote_service -q
# 6 passed
```

状态：远端多租户隔离从 workspace/planner/memory 扩展到 Gateway 进程内 runtime state。剩余建议：继续跑全量测试、integration smoke、`git diff --check` 与 secret grep。

补充稳定性处理：`src/gateway_persistence.py` 中 tool cache 在 persistence 未初始化时不再输出 error 日志，直接静默降级为 memory cache。这样 integration/smoke 或嵌入式调用未启动完整 `gateway_app` persistence lifecycle 时，不会出现误导性的 `Database not initialized` 错误噪声。

## 2026-06-26 Streaming Planner observability metadata

目标：让 streaming 路径也具备非 streaming 路径已经有的 Agent Planner 可观测性。chat-only upstream 只负责最终文本 synthesis，外层 Planner 的 workflow/step/evidence metadata 必须能随最终响应暴露给客户端或调试工具。

修改：

- `src/gateway_streaming.py`
  - `_stream_gateway_context(response)` 提取 response 上的 `gateway_context`。
  - `_attach_stream_gateway_context(payload, gateway_context)` 把 metadata 附到现有终止/完成 chunk。
  - Chat Completions：final `chat.completion.chunk` 携带顶层 `gateway_context`。
  - Anthropic Messages：`message_delta` 携带顶层 `gateway_context`。
  - OpenAI Responses：`response.completed.response.gateway_context` 携带 metadata。
  - 设计约束：不新增自定义 SSE event，降低严格 SDK/客户端 parser 的兼容风险。
- `tests/test_gateway.py`
  - `test_stream_final_response_carries_gateway_context_metadata` 覆盖三种协议的 streaming metadata 输出。

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_carries_gateway_context_metadata \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_has_message_start \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_responses_has_item_before_text_delta \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 4 passed
```

状态：streaming / non-streaming 都能暴露 planner metadata。后续可继续补更细粒度的 progress telemetry，但当前实现优先保持协议兼容。

## 2026-06-26 Agent Planner state snapshot metadata

目标：把外层 Agent Planner 从“返回下一步工具调用的 shim”推进为可审计的远端多轮 runtime。每轮 planner 决策应携带 workflow state snapshot，客户端才能知道 planner 已完成哪些步骤、积累了多少 evidence、是否发生过压缩。

修改：

- `src/gateway_agent_planner.py`
  - `planner_state_snapshot(state, max_summary_chars=1200)`：输出 bounded metadata，不暴露完整 sqlite state。
  - `prepare_upstream_body()`：final synthesis 前写入：
    - `gateway_context.agent_planner.workflow`
    - `gateway_context.agent_planner.step`
    - `gateway_context.agent_planner.state`
    - `gateway_context.planner_evidence_chars`
    - `gateway_context.strategy=agent_planner_final_synthesis`
- `src/gateway_tool_runtime.py`
  - downstream tool request response 的 `gateway_context.agent_planner` 新增 `state` snapshot。
- `tests/test_gateway.py`
  - 覆盖 tool request 中的 `state.current_step/evidence_count/completed_steps`。
  - 覆盖 final synthesis request/response 中的 `state.evidence_summary_preview` 和 strategy propagation。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis -q
# 3 passed
```

状态：Planner 状态现在能随响应/stream 传播。后续可以在这个 state snapshot 基础上继续加 UI progress、admin status endpoint 或更细粒度 telemetry。

兼容性修正：`prepare_upstream_body()` 现在只在没有既有 `gateway_context.agent_planner` 时创建 final-synthesis planner context；如果请求已经来自 Gateway-owned tool preexecute（如 calculator/HTTP Action/MCP），会保留原 `workflow=gateway_owned_tool` / `tool` / `success` 字段，避免 state snapshot 覆盖服务端工具预执行 metadata。

## 2026-06-26 Agent Planner admin status endpoint

目标：远端 Agent Planner runtime 需要服务端可观测 API。响应内 metadata 只能看到当前请求；运维/admin 需要查询最近 planner sessions，确认 workflow、current step、evidence、compaction 状态。

修改：

- `src/gateway_agent_planner.py`
  - `AgentPlannerStore.list_recent(limit=50)`：只读查询最近 planner sessions。
  - 返回 `planner_state_snapshot()`，避免暴露完整 evidence summary。
  - `limit` hard clamp 到 1..500。
- `src/gateway_http_handler.py`
  - 新增 `GET /admin/agent-planner.json?limit=50`。
  - 复用 admin Basic Auth。
  - 返回 `{ "sessions": [...] }`。
- `tests/test_gateway.py`
  - `test_admin_agent_planner_endpoint_lists_runtime_sessions` 覆盖真实 planner state 写入 + admin endpoint 查询。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_mcp_health_endpoint_supports_probe_query \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result -q
# 3 passed
```

状态：Planner runtime 具备只读 status API。下一步可继续补 Admin UI 展示、tenant/workspace/session 过滤、或 progress telemetry。

## 2026-06-26 Agent Planner admin filters for remote multi-tenant runtime

目标：按“远端服务”要求补齐 Agent Planner 状态面的检索能力。服务端同时承载多个用户、多个 client workspace 时，admin endpoint 必须能定位特定 tenant/session/workflow，而不是只返回最近 N 条。

修改：

- `src/gateway_agent_planner.py`
  - `AgentPlannerStore.list_recent()` 支持：
    - `workflow`
    - `current_step`
    - `session_contains`
    - `tenant_contains`
    - `has_evidence`
  - 过滤时 bounded scan：默认扫描 `max(limit * 20, 500)`，上限 5000；返回仍 clamp 到 1..500。
  - 返回内容仍为 `planner_state_snapshot()`，不暴露完整 evidence/state_json。
- `src/gateway_http_handler.py`
  - `GET /admin/agent-planner.json` 读取上述 query filters。
  - 响应包含 `sessions`、`filters`、`limit`。
- `tests/test_gateway.py`
  - 扩展 admin planner endpoint 测试，验证：
    - 未认证请求返回 401。
    - `workflow/current_step/session_contains` 能定位 project_analysis session。
    - `tenant_contains + has_evidence=1` 能定位另一 tenant 的有证据 session。
    - `has_evidence=0` 能定位尚未产生 evidence 的 planner session。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_http_handler.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions -q
# 1 passed
```

状态：Agent Planner admin API 现在具备远端多租户排障所需的最小过滤能力。后续可把这些 filters 接入 Admin UI，或在 planner state 中增加 explicit tenant/workspace fields 以减少对 session_key substring 的依赖。

验证补充：

```bash
python3 -m pytest -q
# 940 passed, 2 skipped, 21 warnings in 45.49s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal
# no output
```

## 2026-06-26 Agent Planner indexed store schema

目标：把 planner session 存储从兼容型 `session_key + state_json` 推进为远端 runtime 可查询状态表。多用户并发服务需要按 tenant/workspace/workflow/current_step/evidence 快速定位 session，不能长期依赖字符串 contains。

修改：

- `src/gateway_agent_planner.py`
  - `planner_sessions` schema 新增：`tenant_key`、`workspace_key`、`workflow`、`current_step`、`evidence_count`。
  - `_init_db()` 自动迁移旧表，保留原 `session_key/state_json/updated_at`。
  - bounded backfill：初始化时最多补 5000 条旧 session 的索引列。
  - `save()` 同步写 state JSON 和索引列。
  - `list_recent()` 改用 SQL WHERE 过滤：
    - `workflow=?`
    - `current_step=?`
    - `LOWER(session_key) LIKE ?`
    - `LOWER(tenant_key) LIKE ?`
    - `evidence_count > 0 / <= 0`
  - 新增 SQLite indexes：
    - `idx_planner_sessions_tenant_workspace_updated`
    - `idx_planner_sessions_workflow_step_updated`
- `src/gateway_http_handler.py`
  - admin endpoint 继续保持相同 query API，但返回 snapshot 现在含 `tenant_key/workspace_key`。
- `tests/test_gateway.py`
  - 覆盖旧 schema migration + backfill。
  - 覆盖新 save 写 explicit tenant/workspace 索引。
  - 覆盖 admin endpoint 返回 tenant_key。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_agent_planner_store_migrates_and_indexes_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_mcp_health_endpoint_supports_probe_query \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result -q
# 4 passed
```

状态：Planner store 现在具备远端多租户 runtime 最小可运维索引面。下一步可以在 Admin UI 中直接暴露这些 filters，或把 conversation memory store 也按同样方式补显式 tenant/workspace/session columns。

验证补充：

```bash
python3 -m pytest -q
# 941 passed, 2 skipped, 21 warnings in 45.63s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal
# no output
```

## 2026-06-26 Conversation memory indexed store schema

目标：无限上下文不仅要能总结，还必须适合远端多用户服务。conversation memory / periodic rollup 需要与 Agent Planner store 一样具备显式 tenant/workspace/session 索引，避免长上下文总结跨用户、跨 workspace、跨 session 注入。

修改：

- `src/gateway_logging.py`
  - `conversation_memories` 新增列：`tenant_key`、`workspace_key`、`memory_session_key`。
  - `_sqlite_init()` 自动迁移旧 SQLite 并建立索引：
    - `idx_conversation_memories_scope`
    - `idx_conversation_memories_kind_scope`
- `src/gateway_context.py`
  - `_memory_session_index_parts()` / `_memory_index_fields()`：从 memory session key 和 workspace root 派生显式远端 scope。
  - `_sqlite_backfill_memory_index_fields()`：bounded backfill 升级前 memory rows。
  - `_sqlite_insert_memory()` 写入显式索引列。
  - recall/search/rollup 查询改用 `tenant_key + workspace_key + memory_session_key`。
  - `_sqlite_tail_memories()` 返回显式 scope 字段，便于 admin status/debug。
- `tests/test_gateway.py`
  - `test_conversation_memory_store_migrates_and_indexes_remote_scope` 覆盖旧 schema migration/backfill、新行索引写入、远端 scope recall。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_store_migrates_and_indexes_remote_scope \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_recalls_same_session_workspace_only \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_compacts_huge_turns_in_sqlite -q
# 4 passed

python3 -m pytest -q
# 942 passed, 2 skipped, 21 warnings in 46.37s
```

状态：无限上下文记忆层具备远端多租户索引和旧库兼容迁移。下一步可把 `/admin/memories.json` 扩展为支持 tenant/workspace/session filters，并在 Admin UI 中暴露 rollup/recall 可观测面。

## 2026-06-26 Admin memory filters and UI observability

目标：无限上下文 memory/rollup 已经按 tenant/workspace/session 索引，但远端服务还需要可查询状态面。Admin/API 必须能定位某个用户、workspace、session 的记忆和周期总结。

修改：

- `src/gateway_context.py`
  - `_sqlite_tail_memories()` 支持：
    - `tenant_contains`
    - `workspace_contains`
    - `session_contains`
    - `kind`
    - `has_rollup`
  - 使用 SQL WHERE 查询 indexed scope。
  - `limit` clamp 到 1..500。
- `src/gateway_http_handler.py`
  - `GET /admin/memories.json` 支持上述 filters。
  - 返回 `memories`、`filters`、`limit`。
- `src/gateway_admin.py`
  - 对话记忆表格显示 Tenant / Workspace / Session。
  - 显示 filter API 示例。
- `tests/test_gateway.py`
  - `test_admin_memories_endpoint_filters_remote_scope` 覆盖鉴权和 scope filters。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_memories_endpoint_filters_remote_scope \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_store_migrates_and_indexes_remote_scope -q
# 2 passed

python3 -m pytest -q
# 943 passed, 2 skipped, 21 warnings in 47.09s
```

状态：无限上下文的存储、周期总结、回忆和 admin 可观测面都已经具备远端多租户 scope。下一步可继续把 Agent Planner 和 Memory 的状态整合成统一 `/admin/agent-runtime.json`，或补真正的 workflow event/progress timeline。

## 2026-06-26 Unified Agent Runtime admin status

目标：把分散的 Planner 状态与 Infinite Context memory/rollup 状态合并成统一远端 runtime API。远端多用户服务需要一次查询即可确认 workflow/evidence/memory/rollup 是否属于同一 tenant/session/workspace。

修改：

- `src/gateway_http_handler.py`
  - 新增 `GET /admin/agent-runtime.json`。
  - 支持 filters：`tenant_contains`、`workspace_contains`、`session_contains`、`workflow`、`current_step`、`memory_kind`/`kind`、`has_evidence`、`has_rollup`、`limit`。
  - 聚合返回：
    - `runtime.agent_planner.sessions`
    - `runtime.agent_planner.session_count`
    - `runtime.agent_planner.active_workflows`
    - `runtime.memory.memories`
    - `runtime.memory.memory_count`
    - `runtime.memory.rollup_count`
- `src/gateway_admin.py`
  - Admin UI 新增 Agent Runtime 状态卡片和 API 示例。
- `tests/test_gateway.py`
  - `test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope` 覆盖鉴权、scope filter、planner/memory 聚合、避免其他 tenant 数据泄露。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope -q
# 1 passed

python3 -m pytest -q
# 944 passed, 2 skipped, 21 warnings in 48.45s
```

状态：Agent Runtime 具备统一 admin status surface。下一步可继续补 workflow event/progress timeline，把每个 planner step、tool dispatch、tool result、evidence compaction、memory rollup 写入同一 timeline，以更接近完整 Agent Runtime。

## 2026-06-26 Agent Runtime event timeline

目标：统一 runtime snapshot 只能回答“现在是什么状态”，不能回答“怎么到这个状态”。远端 Agent Runtime 需要 timeline 追踪 planner step、evidence 状态变更、memory rollup 生成等关键事件。

修改：

- `src/gateway_agent_planner.py`
  - 新增 SQLite 表 `runtime_events`。
  - `AgentPlannerStore.save()` 写 `planner_state` event。
  - 新增 `record_runtime_event()` / `list_runtime_events()`。
- `src/gateway_context.py`
  - `session_rollup` 写入时记录 `memory_rollup` event。
- `src/gateway_http_handler.py`
  - `/admin/agent-runtime.json` 增加 `runtime.events`。
  - 新增 `/admin/agent-runtime-events.json`，支持 scope/event/workflow/step filters。
- `src/gateway_admin.py`
  - Admin UI 展示 event timeline API 提示。
- `tests/test_gateway.py`
  - Runtime endpoint 测试覆盖 `planner_state` 和 `memory_rollup` event 查询。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope -q
# 1 passed

python3 -m pytest -q
# 944 passed, 2 skipped, 21 warnings in 49.34s
```

状态：远端 Agent Runtime 具备 snapshot + timeline 两类可观测面。下一步可继续把具体 tool dispatch / tool result / evidence compaction 也写成细粒度事件，而不是只记录 planner_state 聚合事件。

## 2026-06-26 Remote Runtime event coverage for service-owned and fallback dispatch

状态：完成。

目标：按“远端多租户 Agent Planner Runtime”重新校验 runtime timeline。Gateway-owned service tools 与非 planner fallback downstream dispatch 不能成为不可观测路径；所有事件必须归属到 tenant/session/client workspace，而不是 Gateway 服务机 cwd。

修改：

- `src/gateway_tool_runtime.py`
  - 新增 request-scoped event helper，复用 Agent Planner session key。
  - Gateway-owned preexecute 记录 `gateway_tool_execute` / `gateway_tool_result`。
  - fallback downstream tool request 记录 `tool_dispatch`，workflow=`direct_downstream_tool_request`。
- `tests/test_gateway.py`
  - 覆盖 service-side calculator preexecute event scope。
  - 覆盖无声明工具项目分析 fallback dispatch event scope。

验证：

```bash
python3 -m py_compile src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_direct_downstream_fallback_records_remote_runtime_event -q
# 2 passed

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_direct_downstream_fallback_records_remote_runtime_event \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_periodically_compacts_evidence_with_llm_summary \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_builtin_calculator_preexecutes_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_parallel_direct_tool_calls_keep_client_workspaces_isolated \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_tenant_session_scoped -q
# 10 passed
```

### Full verification after remote runtime event coverage

```bash
python3 -m pytest -q
# 947 passed, 2 skipped, 21 warnings in 47.82s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal excluding .gateway_runtime/.gateway_service.json/.case.txt
# no output
```

## 2026-06-26 Admin UI renders Agent Runtime Events

状态：完成。

目标：远端 Agent Planner Runtime 需要可视化排障面。之前已有 `/admin/agent-runtime-events.json`，但 Admin UI 只显示 API 提示，不能直接看到 timeline。

修改：

- `src/gateway_admin.py`
  - Admin UI 读取最近 30 条 runtime events。
  - “兼容性 / Agent Runtime”页新增 `Agent Runtime Events` 表格。
  - 表格展示 event_type、workflow、step、tenant、workspace、summary。
- `tests/test_gateway.py`
  - `test_admin_ui_renders_agent_runtime_events_table` 覆盖 UI 直接渲染 runtime events。

验证：

```bash
python3 -m py_compile src/gateway_admin.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_ui_renders_agent_runtime_events_table \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope -q
# 3 passed
```

## 2026-06-26 Planner intent sanitizer for recalled memory blocks

状态：完成。

目标：无限上下文 memory/rollup 应该增强最终回答，但不能污染 Agent Planner 的当前意图解析。旧 rollup 中的路径、测试、修复、分析关键词不能触发当前请求的工具调度。

修改：

- `src/gateway_agent_planner.py`
  - 新增 `_strip_recalled_memory_blocks()`。
  - `plan_downstream_tool_request()` 使用 memory-stripped user/conversation text 做 intent 判断。
  - `_generic_intent_decision()` 防御式剥离 recalled memory。
- `tests/test_gateway.py`
  - 覆盖 remembered old path 不触发当前工具。
  - 覆盖当前请求路径优先于 recalled memory path。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_recalled_memory_for_current_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_current_request_over_recalled_memory_paths \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction -q
# 3 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_recalls_same_session_workspace_only -q
# 5 passed
```

## 2026-06-26 Planner anonymous session anchor ignores recalled memory

状态：完成。

目标：recalled memory 应增强最终 synthesis，但不能影响 Planner session identity。匿名请求没有 explicit session_id 时，Planner 使用当前用户请求作为 anchor；这个 anchor 必须忽略 Gateway 注入的 memory block，否则同一个当前请求会因为历史 rollup 内容不同生成不同 session key。

修改：

- `src/gateway_agent_planner.py`
  - `_planner_anchor_text()` 对 messages、responses string input、fallback text 全部剥离 `[Gateway recalled memory]` block。
- `tests/test_gateway.py`
  - 覆盖 `/v1/messages` anon session key 不受 recalled memory 内容影响。
  - 覆盖 `/v1/responses` anon session key 不受 recalled memory 内容影响。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_stays_stable_across_tool_result_turns \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_ignores_recalled_memory_anchor_noise \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_responses_session_key_ignores_recalled_memory_anchor_noise \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_recalled_memory_for_current_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_current_request_over_recalled_memory_paths -q
# 5 passed
```

## 2026-06-26 Chat-only final synthesis strips upstream tool surfaces

状态：完成。

目标：外层 Agent Planner 负责工具/能力调度，chat-only upstream 只做最终 synthesis。最终 synthesis 阶段不能再把 native `tools`、`tool_choice` 或 text-tool adapter 手册交给上游，否则会退回“gateway/shim 让弱模型猜工具”的旧模式。

修改：

- `src/gateway_tool_runtime.py`
  - 新增 `_chat_only_synthesis_body()`。
  - weak/chat-only upstream 路径最终 request 跳过 `_merge_builtin_tools()`。
  - 移除 `tools/tool_choice`，并在 `gateway_context` 标记 `chat_only_synthesis`、`upstream_tools_stripped`。
- `tests/test_gateway.py`
  - final synthesis 测试增加工具剥离断言。

验证：

```bash
python3 -m py_compile src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_builtin_calculator_preexecutes_without_request_tools -q
# 3 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_project_analysis_without_path_surfaces_native_tool_fanout_before_upstream \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 4 passed
```

## 2026-06-26 Streaming chat-only final synthesis strips upstream tool surfaces

状态：完成。

目标：chat-only upstream 只做最终 synthesis 的边界必须覆盖 streaming 和 non-streaming。非 streaming 已剥离工具，本轮补齐 streaming path。

修改：

- `src/gateway_streaming.py`
  - `_run_streaming_orchestration_scoped()` 在 weak/chat-only upstream 下使用 `_chat_only_synthesis_body()`。
  - final synthesis request 移除 `tools/tool_choice` 并标记 `chat_only_synthesis`。
- `tests/test_gateway.py`
  - streaming symbol deep-dive final synthesis 测试增加工具剥离断言。

验证：

```bash
python3 -m py_compile src/gateway_streaming.py src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream -q
# 3 passed
```

## 2026-06-26 Chat-only final synthesis no longer parses upstream tool attempts

状态：完成。

目标：即使 final synthesis request 不带 `tools/tool_choice`，弱上游仍可能在文本里输出 JSON tool request。Agent Runtime 不能再解析这些内容为工具调用，否则 chat-only 模型重新获得工具调度权。

修改：

- `src/gateway_tool_runtime.py`
  - 新增 `_chat_only_synthesis_active()`。
  - non-streaming final synthesis 阶段跳过 tool extraction / text tool fallback / intent fallback。
  - `_attach_request_gateway_context()` 透传 `chat_only_synthesis` / `upstream_tools_stripped`。
- `src/gateway_streaming.py`
  - streaming final synthesis 阶段同步跳过 tool extraction。
- `src/gateway_agent_planner.py`
  - 移除 final synthesis prompt 中要求上游输出 JSON Edit 的旧指令。
- `tests/test_gateway.py`
  - 覆盖 JSON `Edit` 只能作为文本返回。
  - 覆盖 fix_loop JSON patch 不再被授予工具权限。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_final_synthesis_ignores_upstream_json_tool_request \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_fix_loop_upstream_patch_json_is_not_granted_tool_authority \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 4 passed
```

## 2026-06-26 Ignored upstream tool attempts are observable

状态：完成。

目标：chat-only final synthesis 阶段已经禁止弱上游重新获得工具调度权；本轮补齐远端服务运维可观测性，便于发现弱上游仍在输出 JSON/function-call/tool-use markup 的情况。

修改：

- `src/gateway_tool_runtime.py`
  - 新增 ignored-attempt runtime event：`event_type=upstream_tool_attempt_ignored`。
  - event scope 使用原始 client request 的 tenant/session/workspace，避免上游协议转换后丢失远端用户范围。
  - metadata 只保存 bounded calls / response preview，`tool_authority_granted=false`。
- `src/gateway_streaming.py`
  - streaming chat-only final synthesis 同步记录 ignored event。
- `tests/test_gateway.py`
  - 覆盖 non-streaming 与 streaming 两条路径。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_final_synthesis_ignores_upstream_json_tool_request \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 2 passed
```

结论：远端 Agent Runtime 现在能区分“Planner 授权的工具步骤”和“弱上游 final synthesis 中被忽略的伪工具尝试”。后者只进入 runtime timeline，不会触发执行或下游 tool request。

## 2026-06-26 Full regression after chat-only boundary tightening

状态：完成。

复核结论：`tools_enabled=adapter/auto` 只是上游能力模式，不等于当前请求已经进入 Agent Planner final synthesis。最终工具权限硬切断必须只作用在 Planner-owned final turn，否则会破坏 legacy/native orchestration loop。

修正：

- `_should_use_chat_only_synthesis_boundary()` 只在 `gateway_context.strategy=agent_planner_final_synthesis` 或存在 planner-owned context 时启用 chat-only final boundary。
- streaming / non-streaming 统一使用该判定。
- native-capable alias calculator tests 显式使用 native upstream config。
- multi-round smoke 更新为 fail-closed：上游 JSON `Edit` 被记录为 ignored attempt，不被执行。

验证：

```bash
python3 -m pytest -q
# 953 passed, 2 skipped, 21 warnings in 47.89s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=1; ignored_upstream_tool_attempt=Edit
```

## 2026-06-26 Project-scope CLI smoke after boundary fixes

状态：完成。

验证远端服务不能把 Gateway 服务机目录当作用户 workspace；Claude/Codex downstream 均能在 client project scope 内执行。

```bash
python3 tests/integration/project_scope_cli_smoke.py
# pass=true
# claude.ok=true
# codex.ok=true
# memory_service_root_leak=false
# direct_list_leaks_service_skills=false
```

## 2026-06-26 Planner workspace-scoped runtime filtering

状态：完成。

目标：远端服务中同一 tenant 可能同时操作多个 client workspace；Admin Runtime API 必须能用 workspace 精确过滤 planner sessions，不能只过滤 memory/events。

修改：

- `AgentPlannerStore.list_recent(..., workspace_contains=...)` 新增 SQL 过滤，使用 `planner_sessions.workspace_key` 索引列。
- `/admin/agent-planner.json` 支持 `workspace_contains`。
- `/admin/agent-runtime.json` 的 `runtime.agent_planner.sessions` 也按 `workspace_contains` 过滤，和 memory/events 对齐。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope -q
# 2 passed
```

## 2026-06-26 Agent capability registry endpoint

状态：完成。

目标：完整 Agent Planner Runtime 需要可查询的能力注册表，明确哪些能力由 Gateway 服务端执行，哪些能力必须下发到 client workspace，避免远端多用户排障只能读代码或日志。

新增：

- `planner_capability_catalog()`
  - `mode=remote_agent_planner`
  - `chat_only_upstream_role=synthesis_only`
  - `ownership_model.gateway_service / downstream_client / chat_only_upstream`
  - workflows：`project_analysis`、`generic_tool`、`code_search`、`test_build`、`fix_loop`、`edit`、`gateway_owned_tool`、`chat_only_synthesis`
  - service-side capabilities：pure/network/connectors、HTTP Actions、可选 MCP tools
  - downstream-owned capabilities：Read/Bash/Edit/Skill/GUI/local agent 等用户机器工具
- `/admin/agent-capabilities.json`
- `/admin/agent-runtime.json` 内嵌 `runtime.capabilities`

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope -q
# 2 passed
```

## 2026-06-26 Planner decision history in runtime state

状态：完成。

目标：Agent Planner Runtime 需要能解释“为什么当前 step 是这个、最近下发了哪些工具”。单独的 `tool_dispatch` event 不够；session snapshot 也应该保留 bounded decision history。

修改：

- `_append_decision_history()`：每次 planner decision 记录 workflow / step / reason / calls / timestamp。
- `_planner_decision()`：写入 bounded history 后持久化 state，再记录 `tool_dispatch` event。
- `planner_state_snapshot()`：暴露最近 10 条 `decision_history` 和 `last_decision`，只保留工具 id/name 与 bounded reason。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_project_analysis_without_path_surfaces_native_tool_fanout_before_upstream -q
# 2 passed
```

## 2026-06-26 Workflow registry drives planning and capability catalog

状态：完成。

目标：Agent Planner 不应把 workflow step 列表散落在多个模块。需要一个统一 workflow registry，同时驱动 planner Todo/update_plan 和 capability observability。

修改：

- `WORKFLOW_REGISTRY`：集中维护 workflow owner / description / steps / plan_items。
- `planner_workflow_catalog()`：公开 bounded workflow catalog。
- `_planner_plan_items()`：从 registry 读取 plan items。
- `planner_capability_catalog()`：从 `planner_workflow_catalog()` 读取 workflows，不再维护重复 hardcoded list。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_emits_update_plan_before_project_tools_when_declared -q
# 2 passed
```

## 2026-06-26 Planner intent classification observability

状态：完成。

目标：Agent Planner 不能只靠散落 if/else 隐式决定下一步；远端多用户服务需要能在 session snapshot / runtime timeline 中看到当前 intent、workflow、confidence、signals 和 source，便于排障和证明 client workspace 工具没有被旧 memory 或 chat-only upstream 越权触发。

修改：

- 新增 `PlannerIntent` / `classify_planner_intent()`。
- `plan_downstream_tool_request()` 在 dispatch 前持久化 intent。
- `planner_state_snapshot()` 暴露 bounded `intent` / `intent_history`。
- runtime events 新增 `intent_classification`。
- intent 解析会剥离 `[Gateway recalled memory]`，memory 只作为 synthesis evidence，不作为当前指令。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_recalled_memory_for_current_intent -q
# 2 passed
```

## 2026-06-26 Intent registry and concurrent Planner Store hardening

状态：完成。

目标：结构化 intent 不能只存在于 snapshot；Agent Planner 需要正式 intent registry，并让调度路径消费该 intent。同时远端服务必须能承受多个用户同时请求，不能在首请求并发时因为 Planner SQLite store 初始化竞态失败。

修改：

- 新增 `INTENT_REGISTRY` / `planner_intent_catalog()`。
- `planner_capability_catalog()` 暴露 `intents` 与 `counts.intents`。
- `_generic_intent_decision()` 优先消费已分类的 `intent.kind` / `intent.workflow`。
- 新增 `_STORE_LOCK` 保护 `_STORE` 懒加载，避免多线程同时创建多个 `AgentPlannerStore`。
- 新增并发隔离测试，覆盖两个 tenant + 两个 client workspace 同时 planner dispatch。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# 1 passed
```

并发测试证明：两个用户同时请求时，Gateway 下发的 `Read` 目标文件分别落在各自 workspace，persisted planner session 的 `intent.kind=read_file`，`tenant_key/workspace_key` 互不混淆。

## 2026-06-26 Project-analysis transition registry

状态：完成。

目标：`project_analysis` 是最接近原生 coding agent 的核心 workflow，不能继续把 step 顺序写死在一个大函数里。远端 Agent Planner 应该有可查询的状态图和 transition table。

修改：

- 新增 `PROJECT_ANALYSIS_TRANSITIONS`。
- `WORKFLOW_REGISTRY.project_analysis.transitions` 公开 transition 定义。
- `planner_workflow_catalog()` 返回 transition step / condition / builder / reason。
- 新增 `_project_analysis_transition_decision()` 统一执行 transition evaluation。
- `plan_downstream_tool_request()` 对 project-analysis 的主路径改为调用 transition evaluator。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_codebase_search_graph_for_project_structure -q
# 4 passed
```

## 2026-06-26 Generic workflow transition engine

状态：完成。

目标：`project_analysis` transition table 不应有专用 evaluator；否则后续每个 workflow 仍会复制一套状态机。需要一个通用 transition engine，把 workflow-specific 逻辑限制在 condition/builder handlers。

修改：

- 新增 `_workflow_transition_decision()`。
- 新增 handler 类型：`TransitionCondition` / `TransitionBuilder`。
- 新增 `PROJECT_ANALYSIS_CONDITIONS` / `PROJECT_ANALYSIS_BUILDERS`。
- `_project_analysis_transition_decision()` 改为构建 context 后调用通用 engine。
- 删除旧 `_project_analysis_build_calls()`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_codebase_search_graph_for_project_structure \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# 5 passed
```

## 2026-06-26 fix_loop and qa_loop transitions

状态：完成。

目标：通用 transition engine 不能只服务 `project_analysis`；修复闭环也必须进入同一 Agent Planner workflow 模型。

修改：

- 新增 `FIX_LOOP_TRANSITIONS` 和 `QA_LOOP_TRANSITIONS`。
- `WORKFLOW_REGISTRY` 公开 fix/qa transitions。
- 新增 fix/qa condition + builder handler maps。
- `_generic_intent_decision()` 改为通过 `_fix_qa_transition_decision()` 调用通用 transition engine。
- 新增 source followup 测试，验证 diagnostic read 后能继续读取源码 import 关联文件。

验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_fix_loop_reads_source_followup_import_after_diagnostic_read \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_qa_loop_reruns_tests_after_edit_result -q
# passed
```

## 2026-06-26 code_search and test_build transitions

状态：完成。

目标：`code_search` / `test_build` 是远端 Agent Planner 的基础工作流入口，不能继续由 `_generic_intent_decision()` 直接手写工具调用；它们需要和 `project_analysis`、`fix_loop`、`qa_loop` 一样走 registry + transition engine。

修改：

- 新增 `CODE_SEARCH_TRANSITIONS`：`code_search_without_existing_search -> code_search`。
- 新增 `TEST_BUILD_TRANSITIONS`：`validation_test_without_existing_run -> run_test`，`validation_build_without_existing_run -> run_build`。
- `WORKFLOW_REGISTRY` 公开 `code_search.transitions` 和 `test_build.transitions`。
- 新增 code_search/test_build condition + builder handler maps。
- `_generic_intent_decision()` 删除旧 code_search/test_build direct dispatch 分支，改为调用通用 transition engine。
- Admin capability 测试验证这两类 transition 对远端客户端/后台可见。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_code_search_infers_mcp_project_argument \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_run_tests_uses_declared_shell_tool -q
# 3 passed
```

补充验证：

```bash
python3 -m pytest -q
# 956 passed, 2 skipped, 21 warnings in 48.54s

python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/project_scope_cli_smoke.py
# all passed; project_scope smoke confirms claude.ok=true, codex.ok=true, memory_service_root_leak=false

git diff --check
# pass

# secret audit for the provided bearer token literal
# grep returned no output outside ignored local runtime/config files
```

## 2026-06-26 generic_tool and edit transitions

状态：完成。

目标：让 Agent Planner 的普通工具入口也成为 registry + transition table 的一部分，而不是保留在 `_generic_intent_decision()` 的散落 if/else 中。

修改：

- 新增 `GENERIC_TOOL_TRANSITIONS`：覆盖 explicit Skill、Shell、Read、List、WebSearch、caller-declared custom function。
- 新增 `EDIT_TRANSITIONS`：覆盖 bounded Edit / Write。
- `WORKFLOW_REGISTRY` 公开 `generic_tool.transitions` 和 `edit.transitions`。
- `_generic_intent_decision()` 现在主要负责：加载 state/evidence、消费结构化 intent、触发各 workflow transition decision；不再直接手写 generic/edit tool dispatch。
- 有 evidence 后 generic/edit transitions 停止重复发同一工具，转入 chat-only evidence synthesis。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_skill_uses_declared_claude_code_skill_schema \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_repeat_read_after_tool_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_repeat_responses_read_after_function_output \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_web_search_uses_declared_downstream_tool_name \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_custom_function_call_is_surfaced_without_upstream_native_support \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_explicit_edit_uses_declared_edit_tool \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_run_tests_uses_declared_shell_tool \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_code_search_infers_mcp_project_argument -q
# 9 passed
```

补充全量验证：

```bash
python3 -m pytest -q
# 956 passed, 2 skipped, 21 warnings in 48.92s

python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/project_scope_cli_smoke.py
# all passed; project_scope smoke confirms claude.ok=true, codex.ok=true, memory_service_root_leak=false

git diff --check
# pass

# secret audit for the provided bearer token literal: no output outside ignored local runtime/config files
```

## 2026-06-26 Responses memory injection and remote pressure smoke

状态：完成。

目标：补齐“上游上下文有限时，Gateway/Agent Runtime 周期总结并召回”的跨协议可靠性；同时证明远端多用户并发请求不会把 client workspace、planner state、memory rollup 串在一起。

修改：

- `/v1/responses` recalled memory 现在注入 `input`，而不是只写入 `messages`。
  - string input -> `[{role: system, content: memory}, {role: user, content: original}]`
  - list input -> prepend/merge system memory item
- 新增 Responses 回归测试，确认 recalled memory 最终进入 OpenAI Chat upstream payload。
- 新增 `agent_planner_remote_pressure_smoke.py`：并发 6 tenant/workspace，覆盖 downstream tool dispatch、runtime event、periodic memory rollup、recall reinjection 和跨用户不泄漏。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_responses_conversation_memory_is_injected_into_input \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# 3 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6
```

补充全量验证：

```bash
python3 -m pytest -q
# 957 passed, 2 skipped, 21 warnings in 48.79s

python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/project_scope_cli_smoke.py
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# all passed; remote pressure smoke checked 6 users/workspaces, planner sessions, memory rollups, and recall payload isolation

git diff --check
# pass

# bearer token literal audit: no output outside ignored local runtime/config files
```

## 2026-06-26 streaming memory recall and admin runtime pressure verification

状态：完成。

目标：确认无限上下文和远端可观测性不只在非流式路径成立；streaming `/v1/responses` 也必须先召回 memory，再进入上游；admin runtime/memory/events 端点必须能过滤真实运行产生的多 tenant/workspace 数据。

修改：

- 新增 streaming Responses memory recall 回归测试。
- 扩展 `agent_planner_remote_pressure_smoke.py`：压力数据生成后启动真实 admin HTTP server，并查询 runtime/memories/events 三个 endpoint。
- smoke 验证 admin endpoint 不泄漏其他 tenant marker。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_streaming_responses_conversation_memory_is_injected_before_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_responses_conversation_memory_is_injected_into_input \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_memories_endpoint_filters_remote_scope -q
# 4 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true
```

补充全量验证：

```bash
python3 -m pytest -q
# 958 passed, 2 skipped, 21 warnings in 48.92s

python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/project_scope_cli_smoke.py
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# all passed; remote pressure smoke confirms streaming/admin-era runtime isolation checks remain green

git diff --check
# pass

# bearer token literal audit: no output outside ignored local runtime/config files
```

## 2026-06-26 Remote Agent Planner runtime-scope hardening

状态：完成。

目标：按远端服务模型复核 streaming 入口，确保它不是本地增强服务假设；多用户并发下 runtime scope 必须由 tenant/session/workspace 组成，不能只依赖服务 cwd、workspace 或 caller-visible session id。

修正：

- `src/gateway_streaming.py`：streaming 入口进入 `_workspace_scope` 时携带完整 body，与非流式 `run_tool_orchestration()` 对齐。
- `tests/test_gateway.py`：新增 streaming scope 回归，验证 scope 包含 tenant/session/workspace。
- `docs/agent-runtime-architecture.md`：记录远端 Agent Planner / Runtime 方案和当前证据。

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_entry_sets_remote_runtime_scope_from_request_body \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# 4 passed

python3 -m pytest -q
# 959 passed, 2 skipped, 21 warnings in 48.97s

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true

git diff --check
# pass

# bearer token literal audit: no output outside ignored local runtime/config files
```

## 2026-06-26 Long-context remote Agent Runtime hardening

状态：完成。

目标：把“上游上下文有限，Gateway/Agent Planner 定期总结并召回”从单元能力推进到远端多用户 streaming 压力证据。

发现并修复：

- Responses `input` list 压缩路径此前只 summary 旧消息，未裁剪 recent `{role, content: "超长字符串"}`；已修复。
- Planner intent classification 此前可能在 direct dispatch 前处理巨型当前输入；已增加有界 head/tail 文本，避免长上下文请求拖慢 regex/path 分类。
- 新增 `agent_planner_long_context_pressure_smoke.py`，覆盖：4 tenant/workspace、并发大上下文写入、rollup、streaming Responses recall、compaction、跨 tenant 无泄漏。

验证：

```bash
python3 -m pytest tests/test_gateway.py::ContextSummarizationTests::test_responses_input_list_compaction_trims_large_recent_item_content \
  tests/test_gateway.py::ContextSummarizationTests::test_responses_input_list_compaction_keeps_summary_and_recent_items \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_bounds_huge_plain_chat_before_intent_regexes -q
# 3 passed

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; rollups_checked=4; streaming_responses_recall_checked=4; compaction_checked=true; cross_tenant_leak_checked=true
```

## 2026-06-26 Chat-only synthesis boundary observability

状态：完成。

目标：让远端服务可以审计“上游 chat-only 模型只做最终对话综合，工具权限属于 Agent Planner/Runtime”。

修正：

- 新增 runtime event：`chat_only_synthesis_boundary`。
- 非流式和 streaming 最终综合都会在剥离 `tools/tool_choice` 后记录：
  - `workflow=chat_only_synthesis`
  - `step=strip_upstream_tools`
  - `tool_authority_granted=false`
  - `upstream_tools_stripped=true`

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis -q
# 2 passed
```

## 2026-06-26 Agent Runtime requirement audit endpoint

状态：完成。机器可读审计面已补齐，并已通过全量与远端压力回归。

目标：把“远端 Agent Planner / Runtime 是否满足要求”从聊天描述变成可查询的 operator API，避免误把项目理解为本地增强服务。

新增：

- `/admin/agent-runtime-audit.json`
  - 支持 `tenant_contains`、`workspace_contains`、`session_contains`、`workflow`、`current_step`、`event_type`、`memory_kind/kind`、`limit`。
  - 只基于当前 scoped data 生成 audit，不跨 scope 读取证据。
  - 每个 requirement 输出 `status`：
    - `proven/current_scope`：当前 scope 有运行时证据；
    - `configured/static`：静态能力已配置但当前 scope 缺 runtime 证据；
    - `missing/current_scope`：当前 scope 缺必要证据。

覆盖需求项：

1. `chat_only_upstream_synthesis_only`
2. `planner_owns_intent_and_workflows`
3. `downstream_client_workspace_tools`
4. `gateway_owned_service_tools`
5. `infinite_context_memory_rollup`
6. `tenant_workspace_isolation`
7. `streaming_nonstreaming_parity`
8. `admin_observability`

已验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements -q
# 1 passed
```

最终验证补充：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 5 passed

python3 -m pytest -q
# 962 passed, 2 skipped, 21 warnings in 49.86s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1
python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=1; ignored_upstream_tool_attempt=Edit
python3 tests/integration/project_scope_cli_smoke.py
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

## 2026-06-26 Remote pressure smoke covers Agent Runtime audit

状态：完成。

目标：把机器可读审计面纳入真实远端多用户 smoke，避免 `/admin/agent-runtime-audit.json` 只在单元测试中成立。

修改：

- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - 6 tenant / 6 client workspace 并发仍保持；
  - admin scope 额外触发 Gateway-owned `calculator`，产生 service-side tool evidence 和 chat-only final synthesis boundary；
  - HTTP 查询 `/admin/agent-runtime-audit.json`；
  - 验证当前 scope 的核心 requirement 均为 `proven/current_scope`，并确认其他 tenant marker 不进入 audit payload。

验证：

```bash
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true; admin_audit_checked=true
```

复验补充：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements -q
# 1 passed

python3 -m pytest -q
# 962 passed, 2 skipped, 21 warnings in 49.96s

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

## 2026-06-26 Remote pressure audit proves streaming/non-streaming parity

状态：完成。

目标：让远端压力 smoke 不再接受 `streaming_nonstreaming_parity=missing/current_scope`，而是在同一个 tenant/workspace/session scope 内证明 streaming 与非 streaming 都经过 chat-only synthesis boundary。

发现：

- 直接调用 `_run_streaming_orchestration_scoped()` 会绕过正式 workspace resolution，runtime event 落到 `workspace:unavailable`，不能作为远端服务证据。

修正：

- 改为调用正式 `run_streaming_orchestration()`，由请求 body 的 `workspace_root`、metadata user/session 生成 runtime scope。
- pressure smoke 在 admin scope 同时触发非 streaming 与 streaming Gateway-owned `calculator`。
- `/admin/agent-runtime-audit.json` 现在要求全部 8 个 requirement 为 `proven/current_scope`，包括 `streaming_nonstreaming_parity`。

验证：

```bash
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements -q
# 1 passed

python3 -m pytest -q
# 962 passed, 2 skipped, 21 warnings in 49.63s

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

## 2026-06-26 Audit detects legacy gateway passthrough modes

状态：完成。

目标：completion audit 不能只看已有事件；还必须确认当前服务配置确实处于 Agent Planner orchestration mode，而不是旧 gateway passthrough/proxy mode。

发现：

- 旧版 `/admin/agent-runtime-audit.json` 不读取 `gateway.tool_mode`。
- 如果 operator 把服务配置成 `passthrough` / `native_passthrough` / `proxy`，audit 仍可能基于历史事件或静态 catalog 给出过强结论。

修正：

- audit 新增 `agent_planner_runtime_mode` requirement。
- audit payload 新增 `runtime_config`：
  - `gateway_tool_mode`
  - `upstream_tools_enabled`
  - `legacy_gateway_passthrough`
- `gateway.tool_mode in {passthrough,native_passthrough,proxy}` 时，`agent_planner_runtime_mode=missing/current_scope`，整体状态不能为 `proven/current_scope`。
- 远端压力 smoke 要求该 requirement 也为 `proven/current_scope`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode -q
# 2 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true
```

最终验证补充：

```bash
python3 -m pytest -q
# 963 passed, 2 skipped, 21 warnings in 50.28s

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

## 2026-06-26 Audit detects upstream native tool authority

状态：完成。

目标：completion audit 必须确认上游仍是 chat-only synthesis model，而不是通过 config 重新获得 native tools/function-calls 权限。

发现：

- `gateway.tool_mode=orchestrate` 还不够。
- 如果 `upstream.tools_enabled=auto/native` 且 capabilities 同时声明 `supports_tools=true`、`supports_function_calls=true`，旧兼容逻辑会把上游视为 native-tool capable。

修正：

- audit 新增 `chat_only_upstream_config` requirement。
- audit `runtime_config` 新增：
  - `upstream_supports_tools`
  - `upstream_supports_function_calls`
  - `upstream_native_tool_authority`
- native tool authority 被配置出来时，`chat_only_upstream_config=missing/current_scope`，整体不能为 `proven/current_scope`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_upstream_native_tool_authority -q
# 3 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true
```

最终验证补充：

```bash
python3 -m pytest -q
# 964 passed, 2 skipped, 21 warnings in 50.98s

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

## 2026-06-26 Audit detects Gateway-side user-machine tool execution

状态：完成。

目标：远端 Agent Runtime 必须保证 user-machine tools 在 downstream client workspace 执行，不能被旧兼容配置切回 Gateway 服务侧执行。

发现：

- `execute_user_side_tools_in_gateway=true` 是 legacy/local-proxy escape hatch；`delegate_tools_to_downstream=false` 不再授权云端 Gateway 本地执行 user-side tools。
- 如果 audit 不检查这些配置，operator 可能误以为当前仍符合远端 client workspace 边界。

修正：

- audit 新增 `downstream_client_tool_execution_policy` requirement。
- audit `runtime_config` 新增：
  - `gateway_execute_user_side_tools`
  - `gateway_delegate_tools_to_downstream`
  - `gateway_forces_local_user_side_tools`
- 显式开启 `execute_user_side_tools_in_gateway` 强制服务侧执行 user-side tools 时，requirement 标为 `missing/current_scope`，整体不能为 `proven/current_scope`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_gateway_user_side_tool_execution \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_upstream_native_tool_authority -q
# 4 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true
```

最终验证补充：

```bash
python3 -m pytest -q
# 965 passed, 2 skipped, 21 warnings in 51.37s

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

## 2026-06-26 Live regression: chat-only refusal fallback

状态：已修复，服务已重启。

真实客户端测试暴露的问题：Agent Planner 已记录 `project_analysis`、`planner_state=synthesis`、`chat_only_synthesis_boundary`，但上游最终回复仍为通用拒答/闲聊转移，导致用户看到 `Hello, I can't answer this question for now. Let's talk about something else.`。

技术方案：在 Gateway-owned final synthesis boundary 增加响应后处理兜底：

1. 仅对 `agent_planner_final_synthesis` 生效，避免影响普通聊天。
2. 检测 chat-only upstream 通用拒答文本。
3. 使用 planner state / evidence summary 生成 deterministic fallback。
4. 同时覆盖 `/v1/messages`、`/v1/chat/completions`、`/v1/responses`。
5. streaming 与 non-streaming 路径都调用同一个 fallback。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py
python3 -m pytest -q tests/test_gateway.py -k 'chat_only or agent_planner_injects_compact_evidence or agent_planner_does_not_leak_chat_only_refusal'
# 11 passed, 278 deselected
./scripts/mimo_gateway.sh restart
curl -sS http://127.0.0.1:8885/healthz | python3 -m json.tool
# ok=true; mode=orchestrate
```

## 2026-06-26 Strict every-turn Agent Planner mode

状态：已实现。

新增配置：`gateway.agent_planner_strict_every_turn`。

- `true`：远端 chat-only 服务模式。每个沟通都会：
  1. 进入 Gateway Agent Planner intent classification；
  2. 记录 planner state/runtime event；
  3. 注入 Agent Planner evidence/envelope；
  4. strip upstream tools/tool_choice；
  5. 只允许 chat-only upstream 做最终 synthesis。
- `false`：兼容模式。项目分析/已有 planner evidence/Gateway-owned final synthesis 仍走 planner boundary；旧 native/text tool loop 保持可用。

当前 live `.gateway_service.json` 已开启 strict；tracked sample config 默认 false 用于兼容测试。

验证：

```bash
GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 967 passed, 2 skipped, 21 warnings
```

## 2026-06-27 Client injected context sanitizer for Agent Planner

状态：已修复并通过全量回归；live 服务需要重启加载新代码。

问题：真实 Claude/Codex 客户端会把 `<system-reminder>`、`SessionStart`、`PreToolUse` hook、全局 `CLAUDE.md/AGENTS.md` 等运行时上下文放进请求体。旧 planner intent classifier 会读取 raw conversation JSON，因此把这些“客户端注入上下文”误当成当前用户指令。例如用户只输入 `jo`，但注入文本里有 “Run lint, typecheck, tests”，于是 planner 错误进入 `test_build` 并派发 Bash。

方案：

1. 在 `src/gateway_agent_planner.py` 增加 `_strip_client_injected_context()` 与 `_looks_like_client_injected_text()`。
2. `_planner_user_text()` 只返回真实用户可见文本；忽略 tool_result、recalled memory、system-reminder/hook/context_guidance。
3. `_planner_conversation_text()` 只聚合过滤后的非 system 可见文本；当 structured messages 存在但过滤后为空时返回空字符串，不再 fallback 到 raw JSON。
4. `prepare_upstream_body()` 的 intent classification 使用 `_planner_conversation_text()`，避免 strict every-turn envelope 在第二次 prepare 时被本地/客户端注入内容带偏。
5. 保留 planner evidence/rollup 给最终 synthesis 使用；只是禁止这些上下文驱动当前 intent/dispatch。

验证：

```bash
python3 -m pytest -q tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_chat_only_refusal_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_client_injected_user_reminders_for_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_plain_chat_is_wrapped_by_agent_planner_envelope
# 3 passed

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 54.01s
```

## 2026-06-27 Strict audit accepts planner-owned tool-dispatch boundary

状态：已修复并通过全量回归；live 服务需要重启加载新代码。

问题：严格 Agent Planner audit 原先要求每个 scoped session 同时具备 `intent_classification` 与 `chat_only_synthesis_boundary`。这对 plain chat/final synthesis 是正确的，但对 `project_analysis`、`code_search`、`test_build` 等中间轮次不正确：这些轮次应该由 Agent Planner 直接返回 downstream `tool_dispatch`，尚未进入 final chat-only synthesis。

方案：`strict_every_turn_planner_envelope` 的 runtime evidence 改为：每个 session 必须具备 `intent_classification`，并至少进入一种 planner-owned boundary：

- final synthesis：`chat_only_synthesis_boundary`；
- downstream client workspace 工具：`tool_dispatch`；
- Gateway-owned service tool：`gateway_tool_execute` / `gateway_tool_result`。

这样 audit 不会把正常进行中的多轮工具会话误判为 missing，同时仍能抓住未进入 planner 的 legacy passthrough / non-strict session。

验证：

```bash
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_non_strict_every_turn_mode \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_upstream_native_tool_authority \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_gateway_user_side_tool_execution \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_client_injected_user_reminders_for_intent
# 6 passed

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 52.72s
```

## 2026-06-27 Integration smoke strict-mode alignment

状态：已修复并通过。

目标要求是远端服务每个沟通都严格进入 Agent Planner。为避免 pressure smoke 仍在兼容模式下证明旧行为，已把 `tests/integration/agent_planner_remote_pressure_smoke.py` 的临时配置改为 `gateway.agent_planner_strict_every_turn=true`，并把 audit required keys 同步到当前 requirement 集合（包含 `strict_every_turn_planner_envelope`）。

验证矩阵：

```bash
python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; project-analysis multi-step planner path works

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream tool attempt is ignored during chat-only synthesis

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; 4 users; rollup/recall/compaction/streaming/cross-tenant isolation checked

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; 6 users; planner sessions, memory rollups, recall payloads, admin runtime/memories/events/audit, streaming parity checked
```

## 2026-06-27 Live protocol strict planner smoke

状态：已验证。

live 8885 服务验证矩阵：

| Path | Mode | Evidence |
| --- | --- | --- |
| `/v1/chat/completions` | non-stream | `agent_planner_strict_every_turn=true`, `intent.kind=plain_chat`, `strategy=agent_planner_final_synthesis` |
| `/v1/responses` | non-stream | same |
| `/v1/messages` | non-stream | same |
| `/v1/chat/completions` | stream | SSE completed with `data:` / done marker |
| `/v1/responses` | stream | SSE completed with `data:` / response completion marker |

Scoped admin audit for the live protocol tenant:

```text
strict_every_turn_planner_envelope = proven/current_scope
session_count = 5
covered_session_count = 5
missing_session_count = 0
streaming_nonstreaming_parity = proven/current_scope
seen_synthesis_sources = [non_streaming, streaming]
```

Secret audit:

- Real `sk-<redacted-live-key>` literal is only present in ignored local runtime/cache files.
- Tracked hits are fake examples only: `sk-secret-key-12345` and `sk-xxx`.
- `git diff --check` passes.

## 2026-06-27 Persisted strict protocol smoke

状态：已新增并通过。

新增 `tests/integration/agent_planner_protocol_strict_smoke.py`，把 live protocol strict 验证固化为 deterministic integration smoke。该脚本不依赖真实 Mimo 上游，而是启动 fake OpenAI-chat upstream 与本地 Gateway HTTP server，验证：

1. `/v1/chat/completions`、`/v1/responses`、`/v1/messages` 的 non-stream 请求都进入 strict Agent Planner envelope。
2. 同三条路径的 stream 请求都生成 SSE 并完成。
3. 发送给 chat-only upstream 的 6 个请求全部带 `gateway_context.agent_planner_strict_every_turn=true` 与 `chat_only_synthesis=true`。
4. `tools` / `tool_choice` 不会泄漏给 chat-only upstream。
5. Admin audit 证明 `strict_every_turn_planner_envelope` 与 `streaming_nonstreaming_parity`。

验证：

```bash
python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; upstream_calls=6; covered_session_count=6; missing_session_count=0

python3 tests/integration/agent_planner_protocol_strict_smoke.py && python3 tests/integration/agent_planner_remote_pressure_smoke.py
# both ok=true

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 52.66s
```

## 2026-06-27 Strict every-turn cache bypass fix

状态：已修复并通过回归；live 服务需重启后生效。

问题：非流式 plain chat 请求在 HTTP handler 层可能命中 semantic cache，并在进入 `run_tool_orchestration()` 前直接返回。严格 Agent Planner 模式下这会破坏“每个沟通都必须匹配 Agent Planner”：没有新的 intent classification、没有 session/workspace scoped runtime event，也可能返回旧请求的 `gateway_context`。

修复：`src/gateway_http_handler.py` 在 `strict_agent_planner_every_turn()` 为 true 时禁用该 semantic cache 旁路。兼容模式仍保留旧缓存行为。

验证证据：

- `python3 tests/integration/agent_planner_protocol_strict_smoke.py`：`upstream_calls=12`，canonical 与 `/anthropic/v1/*` 的 stream/non-stream 全覆盖，`covered_session_count=12`，`missing_session_count=0`。
- `python3 tests/integration/agent_planner_remote_pressure_smoke.py`：6 用户并发、memory rollup/recall/admin audit/streaming parity 通过。
- `python3 tests/integration/agent_planner_project_analysis_smoke.py`、`agent_planner_multiround_smoke.py`、`agent_planner_long_context_pressure_smoke.py` 均通过。
- `GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q`：`969 passed, 2 skipped, 21 warnings`。

Live reload evidence after restart:

- `jo` request with injected `system-reminder` + declared `Bash` tool returned `end_turn`, no `tool_use`, and `intent.kind=plain_chat` / `workflow=chat_only_synthesis`.
- `分析这套项目` request with `Bash`/`Read` tools returned `stop_reason=tool_use` and planner-dispatched `Bash`.
- Scoped live audit: `strict_every_turn_planner_envelope=proven/current_scope`, `session_count=2`, `covered_session_count=2`, `synthesis_session_count=1`, `dispatch_session_count=1`, `missing_session_count=0`.

## 2026-06-27 Tool-dispatch response intent visibility

状态：已修复并通过回归；live 服务已重启验证。

问题：downstream client tool-dispatch 响应已经由 Agent Planner 产生，但 `gateway_context.agent_planner.intent` 顶层缺失，只有 `state.intent`。这会削弱客户端侧“每个沟通都严格匹配 Agent Planner”的可观测性。

修复：`src/gateway_tool_runtime.py::_direct_downstream_tool_request_response()` 现在把 planner state snapshot 的 `intent` 同步到 `gateway_context.agent_planner.intent` 顶层。

验证：

- targeted pytest：3 passed。
- `agent_planner_project_analysis_smoke.py`、`agent_planner_protocol_strict_smoke.py`、`agent_planner_remote_pressure_smoke.py` 均通过。
- full pytest：`969 passed, 2 skipped, 21 warnings`。
- live `/v1/messages?beta=true`：`分析这套项目` 返回 `tool_use Bash`，并携带顶层 `intent.kind=project_analysis` / `workflow=project_analysis`。

## 2026-06-27 Streaming tool-dispatch planner intent visibility

状态：已验证并补 regression test。

目标：streaming 与 non-streaming 都必须让客户端直接看到本轮 Agent Planner intent，而不是只能从服务端状态推断。

本轮确认并锁定：Anthropic SSE tool-dispatch 的 `message_delta.gateway_context.agent_planner.intent` 顶层包含当前 intent。新增测试覆盖 explicit Bash streaming dispatch：SSE 必须同时包含 `gateway_context`、`intent.kind=shell_command`、`workflow=generic_tool`。

验证：

- targeted pytest：3 passed。
- strict protocol smoke：`upstream_calls=12`，canonical + `/anthropic/v1/*`，stream/non-stream 全覆盖。
- remote pressure smoke：6 users，全量 admin audit/rollup/recall/parity 通过。
- full pytest：`969 passed, 2 skipped, 21 warnings`。
- live SSE：`stream=true` Bash 请求返回 `tool_use Bash`，`message_delta.gateway_context.agent_planner.intent.kind=shell_command`，audit `dispatch_session_count=1`、`missing_session_count=0`。

## 2026-06-27 Gateway-owned Assistants/Threads and client workspace metadata

状态：已修复并通过 live + regression + full pytest。

### 问题

- `/healthz` advertised `/v1/assistants` and `/v1/threads`, but exact POST requests were forwarded to the chat-only upstream and failed.
  - Before fix: 500 due to `UpstreamHTTPError.__init__() got multiple values for argument 'upstream_status'`.
  - After constructor fix but before endpoint ownership fix: structured 502 because upstream expected chat `messages/model`, not Assistants/Threads schemas.
- Remote client workspace metadata using `metadata.workspace` was ignored, causing fallback to the service-side `GATEWAY_WORKSPACE_ROOT`.

### Implementation

- Added `src/gateway_assistants.py` for Gateway-owned exact `POST /v1/assistants` and `POST /v1/threads` compatibility responses.
- Routed these exact paths in `src/gateway_http_handler.py` before generic orchestration/upstream forwarding.
- Fixed curl transport upstream error construction in `src/gateway_proxy.py` and widened `UpstreamHTTPError.detail` type in `src/gateway_errors.py`.
- Extended workspace resolution in `src/gateway_tool_runtime.py` for `metadata.workspace`, `metadata.workspace_dir`, and top-level `workspace`.

### Verification

Live public path matrix on `http://127.0.0.1:8885`:

```text
GET  /v1/models                         -> 200 object=list
POST /v1/messages/count_tokens          -> 200
POST /v1/chat/completions/count_tokens  -> 200
POST /v1/assistants                     -> 200 object=assistant
POST /v1/threads                        -> 200 object=thread
POST /v1/tools/call                     -> 200 object=gateway.tool_result
POST /v1/functions/call                 -> 200 object=gateway.tool_result
```

Live `jo` sanitizer proof:

```text
stop_reason=end_turn
tool_names=[]
intent.kind=plain_chat
workflow=chat_only_synthesis
session_key includes /Users/sanbo/Desktop/ti
Workspace resolved via [session_metadata]: /Users/sanbo/Desktop/ti
strict_every_turn_planner_envelope=proven/current_scope
missing_session_count=0
```

Automated verification:

```text
focused regression: 9 passed
strict protocol smoke: ok=true, upstream_calls=12, missing_session_count=0
remote pressure smoke: ok=true, users=6
full pytest legacy compatibility: 975 passed, 2 skipped, 21 warnings
```

追加 live project-analysis proof：

```text
/v1/messages?beta=true user="分析这套项目" with Bash/Read tools
stop_reason=tool_use
tool_uses=[Bash]
intent.kind=project_analysis
intent.workflow=project_analysis
strict_every_turn_planner_envelope=proven/current_scope
dispatch_session_count=1
missing_session_count=0
```

## 2026-06-27 Public surface smoke for advertised paths

状态：已新增并通过。

新增 `tests/integration/agent_planner_public_surface_smoke.py`，把 `/healthz.supported_paths` 变成可执行合同：health advertised 的每一条 public path 都必须可调用且不得返回 5xx。该 smoke 同时覆盖 canonical paths 和 `/anthropic/v1/*` aliases。

当前检查结果：

```text
advertised_count=21
every advertised path status=200
upstream_calls=6
strict_sessions.covered_session_count=6
strict_sessions.missing_session_count=0
```

覆盖功能类别：

- models list
- token count
- assistants create
- threads create
- chat completions
- responses
- messages
- direct tool/function call
- `/anthropic/v1/*` aliases

该 smoke 是对“每一个功能都必须支持”的最低 public API surface 回归闸门；更深层多轮 planner/tool/runtime 行为继续由 strict protocol、project analysis、multiround、long-context 和 remote-pressure smokes 覆盖。

### Direct tool/function workspace ownership in public surface smoke

`agent_planner_public_surface_smoke.py` 已加严 direct tool/function endpoints：

- `/v1/tools/call`
- `/v1/functions/call`
- `/tools/call`
- `/anthropic/v1/tools/call`
- `/anthropic/v1/functions/call`

这些路径现在执行 `Read(surface-client.txt)`，并通过 `metadata.workspace` 指向 client workspace。smoke 同时在 service workspace 放置同名诱饵文件，断言返回内容只能来自 client workspace：

```text
CLIENT_WORKSPACE_OK present
SERVICE_WORKSPACE_SHOULD_NOT_BE_USED absent
Workspace resolved via [session_metadata]: .../client-workspace
```

这把 direct tool/function API 的 workspace 归属纳入 public surface 回归闸门。

## 2026-06-27 Direct tool/function runtime audit boundary

状态：已修复并通过 public surface smoke。

Direct tool/function endpoints 是非聊天 API，但仍属于远端 Gateway 的 public communication surface。它们现在不仅可调用、且 workspace 正确，还会写入 Agent Runtime events：

- `direct_tool_execute`
- `direct_tool_result`

事件字段包括：

- tenant/session/workspace scoped keys；
- `source=direct_tool_endpoint`；
- `owner=gateway_service`；
- `tool_names`；
- `success`。

`/admin/agent-runtime-audit.json` 的 `gateway_owned_service_tools` requirement 现在把 direct tool events 纳入 evidence。public surface smoke 断言：

```text
direct_tool_result_event_count=5
gateway_owned_service_tools=proven/current_scope
workspace_key contains client-workspace
```

## 2026-06-27 Direct tool/function invalid-input handling

状态：已修复并通过 public surface smoke。

无效 direct tool/function 请求现在不会退化为 500。缺失 tool/function name 时：

```text
HTTP 400
error.detail.failure_type=invalid_input
runtime event=direct_tool_error
workspace scope=client workspace
```

这保证 direct endpoint 的失败路径也有稳定 API 语义与 Runtime audit evidence。

## 2026-06-27 Tool result cache workspace isolation

状态：已修复；focused 并发测试 30 次通过。

问题：tool result cache 对 `Read` 等 cacheable read-only tools 只按 tool name + arguments 生成 key，未包含 workspace。并发多用户请求中，相同 `Read(marker.txt)` 参数可能跨 client workspace 复用缓存结果。

修复：cache key arguments 追加内部 sentinel：

```text
__gateway_workspace_cache_key = str(_workspace_root())
```

该字段只参与 cache key，不传入实际工具参数。

验证：

```text
parallel direct workspace isolation test x30 = pass
```

## 2026-06-27 metadata.tenant tenant scoping fix

状态：已修复并通过 live/scoped audit 验证。

问题：远端多用户请求常见字段是 `metadata.tenant`，但此前 tenant 解析只接受 `tenant_id/account_id/organization_id/user_id/user`。因此这类请求会被记录成 `tenant:anonymous`，导致：

- scoped audit 查不到对应用户；
- runtime events 多租户证据不完整；
- 匿名隔离 workspace 不能按该 tenant 稳定分桶。

修复范围：

- Planner session key：`src/gateway_agent_planner.py::_tenant_key_from_body()`；
- Memory session key：`src/gateway_context.py::_tenant_key_from_body()`；
- Runtime/direct tool anonymous workspace：`src/gateway_tool_runtime.py::_isolated_workspace_for_request()`；
- Runtime event scope：`src/gateway_tool_runtime.py::_request_scope_from_body()`；
- Config defaults/templates：strict every-turn 默认 true。

Live 验证结果：

```text
metadata.tenant=live-tenant-alias-user
metadata.workspace=/Users/sanbo/Desktop/ti
user=jo with declared Bash tool and injected PreToolUse reminder

stop_reason=end_turn
tool_names=[]
intent.kind=plain_chat
session_key contains tenant:live-tenant-alias-user
/admin/agent-runtime-events.json tenant_key=live-tenant-alias-user
/admin/agent-runtime-audit.json strict_every_turn_planner_envelope=proven/current_scope
missing_session_count=0
```

Regression：

```text
3 focused tenant alias tests passed
agent_planner_protocol_strict_smoke.py ok=true, 12/12 sessions covered
agent_planner_public_surface_smoke.py ok=true, 21 advertised paths covered
```

## 2026-06-27 count_tokens runtime audit boundary

状态：已修复并 live 验证。

`count_tokens` 是 public API surface，不是聊天 synthesis，也不是 downstream client tool，但仍是远端 Gateway 的一次公开通信。此前它只返回：

```json
{"input_tokens": <int>}
```

但没有 Runtime event，因此 operator 无法在 admin audit 中证明该请求的 tenant/workspace/session 边界。

现在：

- `/v1/messages/count_tokens`
- `/v1/chat/completions/count_tokens`
- `/anthropic/v1/messages/count_tokens`
- `/anthropic/v1/chat/completions/count_tokens`

都会记录：

```text
token_count_execute
token_count_result
```

并且 event 使用同一套 request-scoped tenant/workspace/session 解析。

Live 验证：

```text
metadata.tenant=live-token-count-user
metadata.workspace=/Users/sanbo/Desktop/ti
response.input_tokens=54
runtime event token_count_result workspace_key=/Users/sanbo/Desktop/ti
/admin/agent-runtime-audit.json gateway_owned_service_tools=proven/current_scope
```

Public surface smoke 新增断言：

```text
token_count_result_event_count=4
all token_count events source=token_count_endpoint
all token_count events success=true
all token_count events workspace_key contains client-workspace
```

## 2026-06-27 models/assistants/threads runtime audit boundary

状态：已修复并 live 验证。

在继续执行“每一个公开沟通都必须可证明进入 Gateway Runtime 边界”的审计时，发现剩余 public surface blind spot：

- `/v1/models`
- `/v1/assistants`
- `/v1/threads`
- 对应 `/anthropic/v1/*` aliases

这些路径可调用，但此前只写 request log，缺少 Agent Runtime events。现在它们写入：

```text
models_result / models_error
assistants_result
threads_result
```

这些事件纳入 `gateway_owned_service_tools` requirement audit，并使用统一 request scope：tenant、workspace、session。

Live 验证：

```text
models_result tenant_key=live-public-owned-user workspace_key=/Users/sanbo/Desktop/ti model_count=6
assistants_result tenant_key=live-public-owned-user workspace_key=/Users/sanbo/Desktop/ti object=assistant
threads_result tenant_key=live-public-owned-user workspace_key=/Users/sanbo/Desktop/ti object=thread
```

Public surface smoke 现在覆盖：

```text
models_result_event_count=2
assistants_result_event_count=2
threads_result_event_count=2
token_count_result_event_count=4
direct_tool_result_event_count=5
strict_sessions.missing_session_count=0
```

## 2026-06-27 models error runtime boundary

状态：已修复并纳入 public surface smoke。

`/v1/models` 不只是成功时需要 Runtime evidence；上游模型列表失败时也必须可审计。现在 fake upstream 在 smoke 中强制返回 503，Gateway 行为为：

```text
GET /v1/models?... -> HTTP 502
runtime event=models_error
metadata.owner=gateway_service
metadata.success=false
metadata.failure_type=<exception class>
workspace_key=<client workspace>
```

这确保 client 启动/模型探测阶段的失败也不会成为 Runtime blind spot。


---

## 2026-06-27 Agent Planner 同会话注入上下文污染修复

真实客户端测试发现：`<system-reminder>` / `PreToolUse` / `SessionStart` 与 recalled memory 可能污染下一轮 planner intent，导致 `分析这套项目` 退化为 chat-only synthesis。当前已修复并加入 `tests/test_agent_planner_client_context.py`。

当前 live 行为：
- `jo` + injected runtime context：`end_turn`，不触发工具。
- 同一 session `分析这套项目`：返回 protocol-level `Bash tool_use`，由下游 client workspace 执行。
- chat-only upstream 不再接收 `PreToolUse` / `SessionStart` / 旧 workspace 注入文本。

---

## 2026-07-05 Gateway-owned MCP / HTTP Action 云端参数边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

本轮继续按“Gateway 服务在云端，不是用户本地机器”的边界审计 Gateway-owned 工具。结论与实现：

- HTTP Action 仍由 Gateway 服务端执行，但默认不允许 action URL 指向 `localhost`、私网、link-local、reserved/non-global IP，并会检查域名解析结果与 HTTP redirect 目标，避免下游通过 Gateway-owned action 间接访问云端服务内网。确认为管理员配置的内部 action endpoint 时，单个 action 显式设置 `allow_private_network: true`。
- `WebFetch` / `WebBrowser` / `WebSearch` / `web_search_call` 同样是 Gateway-owned 网络工具，默认复用 URL/DNS/redirect 私网阻断；只有管理员配置 `gateway.allow_private_network_tools=true` 或 `GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS=1` 时才允许服务侧私网访问，下游 tool arguments 不能自行开启。
- `image_generation` 的 OpenAI / Pollinations / Hugging Face provider URL 也复用同一私网阻断；私网 provider 只有管理员开启 `allow_private_network_tools` 才可用。下游 `size` 会按 `GATEWAY_IMAGE_MAX_DIMENSION` 截断（默认 2048，硬上限 4096），避免云端服务请求超大生成。
- MCP server 仍由 Gateway 服务端管理和执行，但 `tools/call` 在启动 MCP server 之前先检查下游参数；`path`、`file_path`、`cwd`、`root`、`directory`、`source`、`destination`、`uri` 等字段中出现 `/etc/passwd`、`../...`、`file:///...`、`src/file.py`、Windows/UNC 路径等服务端文件目标时默认拒绝。
- generic `mcp_call_tool` 与 public `mcp__server__tool` 走同一 `_mcp_call_tool` 校验；`resources/read` / `prompts/get` 也复用同一服务端文件参数校验。
- 确需服务端 filesystem/resource MCP 时，管理员可在 server 或 tool 上显式配置 `allow_service_file_arguments: true`。

验证：

```text
python3 -m compileall -q src tests
direct user-side filesystem/shell/GUI/local-agent/Skill blocking regression: 1 passed
direct multi_tool_use.parallel/parallel nested user-side blocking regression: covered in the same focused acceptance test
focused MCP/HTTP/network/image boundary regression: 18 passed
./scripts/agent_planner_acceptance.sh: PASS, focused pytest inside gate 33 passed
git diff --check
python3 -m pytest -q: 1017 passed, 2 skipped, 21 warnings in 52.49s
```

## 2026-07-05 Gateway-owned Memory 云端租户边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续按“Gateway 服务在云端，不是用户本地机器”审计 Gateway-owned state 工具时，发现 direct `Memory` / `SaveMemory` / `RecallMemory` 的手工记忆默认只按 workspace 过滤，且 `include_all_workspaces` / `all_workspaces` 会在公开 tool endpoint 返回全局列表。这在云端多租户场景下不安全：两个认证 client 可以声明同一个下游 workspace 字符串，或通过公开 tool 参数尝试枚举其它项目/租户记忆。

当前行为：

- public direct Memory 写入会把 `session_key` 归一到当前请求 tenant：优先使用 HTTP 层传入的认证 downstream client scope，而不是 body 里可伪造的 `client_id`。
- public direct Memory 读取同时按当前认证 client tenant 和当前下游 workspace 过滤；同一 workspace 字符串下，不同 downstream client 的手工记忆互不可见。
- `all_workspaces` / `include_all_workspaces` 在 public direct Memory tool 中返回 `permission_denied`；全局审计仍保留在 admin API（需要 admin auth）。
- 底层 `_sqlite_tail_memories()` 新增 exact `tenant_key` 过滤，admin 的 substring filters 不受影响。

Focused 验证：

```text
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_memory_tool_scopes_by_authenticated_client_id_and_blocks_global_listing \
  tests/test_gateway.py::NativeGatewayTests::test_memory_tool_lists_only_active_downstream_project_root \
  tests/test_gateway.py::NativeGatewayTests::test_more_top_tool_aliases_mcp_memory_and_parallel_shapes -q
  3 passed

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_remote_identity_memory_without_scope_does_not_use_gateway_env_root \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_store_migrates_and_indexes_remote_scope \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_recalls_same_session_workspace_only \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_public_helpers_scope_downstream_client_id_without_workspace -q
  4 passed
```

最终验证窗口：

```text
python3 -m compileall -q src tests
./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 33 passed
  Agent Planner acceptance gate: PASS
python3 -m pytest -q
  1017 passed, 2 skipped, 21 warnings in 52.49s
git diff --check
  clean
```

## 2026-07-05 Semantic cache runtime/tenant 边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续按云端 Gateway 边界审计缓存层时，发现非 strict 兼容模式下 HTTP handler 的 semantic cache 仍按最后一条用户文本做 exact / similarity 命中。虽然 strict Agent Planner every-turn 模式会绕过该 cache，但兼容模式仍可能在云端多租户服务中把相同 prompt 的普通 chat response 跨 downstream client / tenant / workspace 复用。

当前行为：

- `SemanticCache.get()` / `put()` 新增可选 `scope_key`，默认空 scope 保持旧单机兼容行为。
- scoped cache key 同时包含 query 和 runtime scope；exact hit 不会跨 scope。
- semantic similarity 慢路径也只在同一 `scope_key` 的 entries 内比较，避免相似 prompt 跨租户命中。
- SQLite `semantic_cache` 新增 `scope_key` 列和索引；持久化 reload 后仍保持同一 scope 隔离。
- HTTP handler 在允许 semantic cache 的兼容模式下，使用 authenticated downstream client + resolved client workspace/session 生成 runtime scope；scope 解析失败时不写入 unscoped semantic cache。

Focused 验证：

```text
python3 -m pytest -q \
  tests/test_semantic_cache.py::TestSemanticCache::test_scope_key_isolates_exact_and_semantic_matches \
  tests/test_cache_persistence.py::TestCachePersistence::test_semantic_cache_persistence_lifecycle \
  tests/test_cache_persistence.py::TestCachePersistence::test_semantic_cache_scope_persists_and_isolates \
  tests/test_persistence.py::TestPersistence::test_semantic_cache_save_and_load
  4 passed

python3 -m compileall -q src tests
./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 36 passed
  Agent Planner acceptance gate: PASS
python3 -m pytest -q
  1020 passed, 2 skipped, 21 warnings in 54.10s
```

## 2026-07-05 Admin Skill/MCP 管理面写入边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计云端服务管理面时发现：`/admin/skill-delete.json` 使用请求体 `name` 直接拼到 `cwd/skills/<name>` 并 `shutil.rmtree()`。虽然这是 admin endpoint，但云端 Gateway 的服务端 filesystem 仍必须有明确边界，不能让路径穿越或跨站 admin POST 误删服务端目录。

当前行为：

- 新增 admin skill name 校验：只允许单段 `[A-Za-z0-9_.-]+` 名称，拒绝空值、`.`、`..`、`/`、`\` 和其它特殊字符。
- skill create / marketplace install / delete 都通过统一 `_admin_skill_dir()` 落到服务端 `skills/` catalog 内。
- `/admin/skill-create`、`/admin/skill-install.json`、`/admin/skill-delete.json`、`/admin/mcp-install.json` 现在都走 `_check_admin_write()`：Basic Auth + browser Origin/Referer 检查。CLI 无 Origin 仍可用；跨站浏览器 POST 会被 403 拒绝。
- 回归证明 `../outside-victim` 不会删除 `skills/` 外目录，恶意 Origin 也不能删除合法 skill。

Focused 验证：

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

## 2026-07-05 Caller-declared builtin 名称归属边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 declared/custom tool schema 到 Gateway-owned/downstream-owned 的归属时发现：如果下游请求声明了一个私有函数，名字刚好叫 `calculator`（或其它 Gateway pure/network builtin 名/别名），此前 chat-only planner 会优先把它当 Gateway 内置工具预执行。这会把调用方声明的私有 schema 误解释成云端 Gateway 的执行权限；在云端部署里，caller-declared 私有工具应归 downstream client，而不是 Gateway 服务端。

当前行为：

- 新增 `_declared_tool_shadows_gateway_builtin()`：识别 caller-declared 私有 schema 与 Gateway builtin 名/别名冲突。
- `_tool_call_requires_downstream_execution()` 在 builtin 执行前先判断这类冲突，默认返回 downstream-owned。
- chat-only Gateway-owned preexecute 会跳过被 caller-declared schema shadow 的 builtin。
- declared function planner 会为这类冲突发出协议级 downstream tool call；没有 caller-declared 冲突时，原本的 Gateway-owned `calculator` 自动预执行保持不变。
- 显式 Gateway extension 仍归 Gateway：`gateway__*` alias、HTTP Action、MCP public name 不被当作 caller-private shadow。

Focused 验证：

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

## 2026-07-05 Upstream routing-field redaction 云端 workspace hint 边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 upstream forwarding 时发现：运行时已经把 `workspace` / `workspace_dir` 当作云端 downstream workspace hint 使用，但转发给上游模型前的 sanitizer 只剥离了 `workspace_root` / `gateway_workspace` / `cwd` 等旧字段。这样非流式或 streaming passthrough 请求可能把客户端项目路径/逻辑 workspace hint 泄露给 chat-only upstream。

当前行为：

- `_GATEWAY_INTERNAL_REQUEST_FIELDS` 新增 `workspace`、`workspace_dir`。
- `_strip_gateway_internal_request_fields()` 会在顶层、`metadata`、JSON 编码的 `metadata.user_id` 中剥离这些字段。
- 普通 `_convert_request_to_upstream()` 与 `_stream_upstream_passthrough()` 复用同一 sanitizer，所以非 streaming 和 streaming 都不会把 workspace routing hint 发给上游。

Focused 验证：

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

## 2026-07-05 Planner envelope upstream redaction 边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 `gateway_context` / `gateway_agent_planner` 后确认：这些字段应该是 Gateway 内部 planner/runtime envelope，只能用于本地 synthesis guard、审计与最终响应/SSE 可观测性，不能作为结构化字段发给 chat-only upstream。上游只需要系统 prompt 中的 bounded planner intent/evidence 摘要。

当前行为：

- non-streaming 已保留 `response_context_body` 供本地 guard 和最终响应使用，再通过 `_convert_request_to_upstream()` 剥离内部 envelope 后发给上游。
- streaming 路径现在同样保留本地 `response_context_body`，用于：
  - 识别 `chat_only_synthesis` boundary；
  - 记录并忽略上游在 final synthesis 阶段输出的伪工具调用；
  - 把 whitelisted `gateway_context` 附到最终 SSE，而不是透传给上游。
- 相关测试改为断言 upstream payload 不含 `gateway_context` / `gateway_agent_planner`，同时 final response / SSE 仍携带 planner metadata。

Focused 验证：

```text
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_final_synthesis_ignores_upstream_json_tool_request \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
  5 passed

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_multiple_builtin_tools_preexecute_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream -q
  4 passed

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
  ok=true; compaction_checked=true; cross_tenant_leak_checked=true

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 47 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1023 passed, 2 skipped, 21 warnings in 53.38s

python3 -m compileall -q src tests
git diff --check
  clean
```

## 2026-07-05 Responses custom tool 双向协议 parity

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 Responses API 多轮工具历史时发现两处不对称：`_append_tool_results()` 已能把 Responses `custom_tool_call` 的结果追加为 `custom_tool_call_output`，`extract_tool_evidence()` 也能读取这类结果；但当下游下一轮仍走 `/v1/responses`、上游协议为 `openai_chat` 时，`_responses_to_chat_payload()` 只处理 `function_call` / `function_call_output`，会把 `custom_tool_call` / `custom_tool_call_output` 历史丢掉。反向上，如果 upstream protocol 是 Responses 且返回 `custom_tool_call`，`_from_responses_response_to_openai()` 也会把它丢掉，导致 Chat downstream 看不到工具调用。

当前行为：

- Responses `function_call` / `tool_call` / `custom_tool_call` 都会转成 Chat `assistant.tool_calls`。
- `custom_tool_call` 的字符串 `input` 会按现有 tool runtime 语义转成 `{"input": "..."}` 的 Chat function arguments。
- Responses `function_call_output` / `custom_tool_call_output` 都会转成 Chat `role=tool` 消息，保留 `call_id` 和 `output`。
- Responses upstream 返回的 `function_call` / `tool_call` / `custom_tool_call` 也会转成 Chat `assistant.tool_calls`。
- 已有 `custom_tool_call` 执行后 append `custom_tool_call_output` 的行为保持不变。

Focused 验证：

```text
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_response_converts_to_chat_tool_call \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_history_converts_to_chat_messages \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_call_executes_and_appends_custom_output -q
  3 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 49 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1025 passed, 2 skipped, 21 warnings in 55.14s
```

## 2026-07-05 Responses→Chat tool-call finish_reason parity

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 Responses upstream → Chat downstream 的协议转换时发现：`_from_responses_response_to_openai()` 已能把 Responses `function_call` / `tool_call` / `custom_tool_call` 转成 Chat `assistant.tool_calls`，但 `choices[].finish_reason` 仍固定为 `stop`。对 Codex / Claude Code 这类 Chat 客户端来说，`finish_reason=stop` 容易被解释成最终回答，导致应该执行的工具轮次不够明确。

当前行为：

- Responses upstream 返回任意 tool-call item 时，Chat downstream 响应同时包含 `assistant.tool_calls` 与 `finish_reason=tool_calls`。
- 没有 tool call、只有普通 `output_text` 时仍保持 `finish_reason=stop`。
- R37 的 custom tool 双向转换保持不变：`custom_tool_call` / `custom_tool_call_output` 仍能在 Responses 与 Chat 历史之间往返。

Focused 验证：

```text
python3 -m pytest \
  tests/test_gateway.py::NativeGatewayTests::test_responses_tool_response_sets_chat_finish_reason_tool_calls \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_response_converts_to_chat_tool_call \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_history_converts_to_chat_messages -q
  3 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 50 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1026 passed, 2 skipped, 21 warnings in 54.95s
```

## 2026-07-05 OpenAI Chat legacy function_call parity

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 Chat API → programmable tool/function-call 支持时发现：现代 OpenAI Chat `tool_calls` 已覆盖得较好，但 legacy `message.function_call` / `finish_reason=function_call` 只被部分检测，未进入完整 runtime/tool extraction 与跨协议转换。结果是某些普通 Chat 上游或旧 SDK 返回 function_call 时，Gateway 可能无法执行 Gateway-owned 工具，也无法把该工具轮次稳定转换给 Anthropic Messages / Responses downstream。

当前行为：

- OpenAI Chat legacy `message.function_call` 会被 `_native_tool_signal()` 与 `_extract_tool_calls()` 识别，进入与现代 `tool_calls` 相同的执行/下发路径。
- Chat legacy function-call 回合执行后，`_append_tool_results()` 追加 legacy `role=function` 结果，避免把旧 function_call 回合与现代 `role=tool` 结果混用。
- OpenAI Chat response → Anthropic Messages 时，legacy `function_call` 转成 `tool_use`，并把 `finish_reason=function_call` 映射成 `stop_reason=tool_use`。
- OpenAI Chat history → Responses 时，assistant `function_call` 转成 `function_call` item，legacy `role=function` 结果转成 matching `function_call_output`。
- OpenAI Chat streaming legacy `delta.function_call` 也会被 streaming parser 识别。
- Chat tool arguments 不是 JSON object 时不再静默丢成 `{}`；转 Anthropic `tool_use.input` 时会包装为 `{"input": ...}`，保留 custom/freeform 工具参数。

Focused 验证：

```text
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_extracts_legacy_chat_function_call_and_appends_function_result \
  tests/test_gateway.py::ProtocolConversionTests::test_openai_chat_legacy_function_call_response_to_anthropic_tool_use \
  tests/test_gateway.py::ProtocolConversionTests::test_openai_chat_tool_call_non_object_arguments_wrap_for_anthropic \
  tests/test_gateway.py::ProtocolConversionTests::test_openai_chat_legacy_function_history_converts_to_responses \
  tests/test_gateway.py::StreamingToolEventTests::test_detect_openai_legacy_function_call_delta
  5 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 55 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1031 passed, 2 skipped, 21 warnings in 52.92s
```

## 2026-07-05 Tool-result error semantics parity

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计多轮工具结果语义时发现：Anthropic Messages 有正式的 `tool_result.is_error`，但转换到 OpenAI Chat / Responses 时没有等价标准字段；此前错误结果会变成普通 `role=tool` / `function_call_output` 文本，Agent Planner evidence 也会把 Chat / Responses tool output 当作成功结果。这会影响修复循环、QA 循环和最终 synthesis：上游/Planner 可能无法区分“工具成功返回内容”和“工具执行失败”。

当前行为：

- 新增 Gateway 内部错误 marker：`[gateway_tool_result_error]
`，仅用于 Chat/Responses 这类没有标准 `is_error` 字段的 tool output 文本通道。
- Anthropic `tool_result.is_error=true` → Chat `role=tool.content` 时会编码 marker；Chat → Anthropic 时会解码并恢复 `is_error=true`，下游 Claude Code 语义不丢。
- Gateway-owned 工具执行失败后，Chat `role=tool` 与 Responses `function_call_output.output` 会带 marker；Anthropic Messages 仍使用原生 `is_error` 字段。
- Agent Planner `extract_tool_evidence()` 会识别 Chat/Responses marker、`is_error` 字段以及 error/failed/incomplete status，把 evidence 标为 `is_error=True`，同时把 marker 从 evidence content 中剥离。
- 普通成功 tool result 不加 marker，既有成功路径输出保持不变。

Focused 验证：

```text
python3 -m pytest -q   tests/test_gateway.py::NativeGatewayTests::test_failed_chat_tool_result_marks_planner_evidence_error   tests/test_gateway.py::NativeGatewayTests::test_failed_responses_tool_output_marks_planner_evidence_error   tests/test_gateway.py::ProtocolConversionTests::test_anthropic_tool_result_error_roundtrips_through_chat_marker
  3 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 58 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1034 passed, 2 skipped, 21 warnings in 53.27s
```

## 2026-07-05 Responses custom_tool_call streaming/detection parity

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 Responses custom tool parity 时发现：非流式 runtime 已支持 `custom_tool_call` / `tool_call`，但两个边缘路径仍只认 `function_call`：

- `_response_has_tool_calls("/v1/responses", ...)` 未把 `custom_tool_call` / `tool_call` 判为工具响应。
- Responses SSE `response.output_item.done` / `response.output_item.added` 解析只接受 `function_call`，导致自定义工具 streaming item 不能被识别；字符串 `input` 也没有规范成 Chat tool-call arguments。

当前行为：

- Responses 通用工具响应判定统一支持 `function_call` / `tool_call` / `custom_tool_call`。
- Responses streaming `output_item.done` / `output_item.added` 同样支持三类工具 item。
- `custom_tool_call` 的字符串 `input` 会保留为 JSON arguments：`{"input": "..."}`；dict input 直接序列化为 arguments object。
- 该修复只补齐 Responses custom/tool streaming 与检测 parity，不改变用户 workspace 云端边界：user-side `Read`/`Bash`/GUI/local-agent/`Skill` 仍默认下发给 downstream client，而不是在 Gateway 本机执行。

Focused 验证：

```text
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_response_has_tool_calls_detects_responses_custom_tool_call \
  tests/test_gateway.py::StreamingToolEventTests::test_detect_responses_custom_tool_call_item
  2 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 60 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1036 passed, 2 skipped, 21 warnings in 55.07s
```

## 2026-07-05 Tool-result error marker final-synthesis redaction

状态：已修复并完成 focused / acceptance / full pytest 验证。

R40 为 OpenAI Chat / Responses 这类没有标准 `is_error` 字段的 tool output 增加了 Gateway 内部 marker，用于在跨协议转换和 Agent Planner evidence 中保留失败语义。继续复核 final synthesis 链路时发现：`extract_tool_evidence()` 已会解码 marker，但 `prepare_upstream_body()` 的最终上游合成消息只清理 user 内容，Chat `role=tool` 与 Responses `function_call_output` 历史仍可能把 `[gateway_tool_result_error]` 原样带给 chat-only upstream。

当前行为：

- Chat `role=tool.content` / legacy `role=function.content` 在进入 final synthesis payload 前会剥离 Gateway 内部错误 marker，只保留真实工具输出文本。
- Responses `function_call_output.output` / `custom_tool_call_output.output` 在进入 final synthesis payload 前同样剥离 marker。
- Planner evidence 仍通过 `_decode_tool_result_content()` 记录 `is_error=True`，不会丢失失败语义。
- 该 redaction 只作用于 Agent Planner final synthesis 的上游 payload；协议 roundtrip 中用于恢复 Anthropic `tool_result.is_error` 的 marker 仍保留在 Chat/Responses 历史通道。

Focused 验证：

```text
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_failed_chat_tool_result_marks_planner_evidence_error \
  tests/test_gateway.py::NativeGatewayTests::test_failed_responses_tool_output_marks_planner_evidence_error \
  tests/test_gateway.py::NativeGatewayTests::test_failed_chat_tool_result_marker_is_not_forwarded_to_final_synthesis \
  tests/test_gateway.py::NativeGatewayTests::test_failed_responses_tool_output_marker_is_not_forwarded_to_final_synthesis \
  tests/test_gateway.py::ProtocolConversionTests::test_anthropic_tool_result_error_roundtrips_through_chat_marker
  5 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 62 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1038 passed, 2 skipped, 21 warnings in 55.13s
```

## 2026-07-05 Legacy Chat role=function planner evidence parity

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 legacy OpenAI Chat function-call 兼容时发现：R39 已经让 `message.function_call` 能被执行，并把结果按旧协议追加为 `role=function`；但 Agent Planner 的 `extract_tool_evidence()` 只识别现代 `role=tool`、Responses `function_call_output` 和 Anthropic `tool_result`，导致 legacy function result 没有进入 planner evidence。这样在 chat-only upstream final synthesis 阶段，旧 Chat API 工具结果可能没有被纳入 planner evidence summary。

当前行为：

- `_assistant_tool_name_by_id()` 会为 legacy `message.function_call` 建立稳定 synthetic call id：`legacy_function_call_<name>`。
- `_assistant_tool_input_by_id()` 会解析 legacy `function_call.arguments` 并写入 evidence 的 `[tool_args:...]` 前缀。
- `extract_tool_evidence()` 会把 `role=function` 结果转换为 `PlannerToolEvidence`，保留 call id、函数名、参数和输出内容。
- 现代 `tool_calls` / Responses / Anthropic evidence 路径保持不变。

Focused 验证：

```text
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_extracts_legacy_chat_function_call_and_appends_function_result \
  tests/test_gateway.py::NativeGatewayTests::test_legacy_chat_function_result_becomes_planner_evidence \
  tests/test_gateway.py::ProtocolConversionTests::test_openai_chat_legacy_function_call_response_to_anthropic_tool_use \
  tests/test_gateway.py::ProtocolConversionTests::test_openai_chat_legacy_function_history_converts_to_responses \
  tests/test_gateway.py::StreamingToolEventTests::test_detect_openai_legacy_function_call_delta
  5 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 63 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1039 passed, 2 skipped, 21 warnings in 55.68s
```

## 2026-07-05 Responses function/custom output planner evidence parity

状态：已修复并完成 focused / acceptance / full pytest 验证。

继续审计 Responses tool-result evidence 时发现：Agent Planner 已能识别 `function_call_output` / `custom_tool_call_output`，但没有从前置 Responses `function_call` / `custom_tool_call` item 建立 name 和 args/input 映射。结果是 final synthesis 证据只剩默认名 `function_call_output` 和输出文本，丢失工具名以及调用参数。

当前行为：

- `_assistant_tool_name_by_id()` 支持 top-level / content-block Responses `function_call`、`tool_call`、`custom_tool_call`，用 `call_id` 映射工具名。
- `_assistant_tool_input_by_id()` 支持 Responses `arguments` JSON / dict，以及 `custom_tool_call.input` 字符串，字符串 input 会保留为 `{"input": ...}`。
- `extract_tool_evidence()` 在处理 `function_call_output` / `custom_tool_call_output` 时会附加 `[tool_args:...]`，与 Chat `role=tool` 和 Anthropic `tool_result` 的 evidence 质量保持一致。
- 该改动只影响 Planner evidence；云端 workspace 归属和用户侧工具默认 downstream 执行边界不变。

Focused 验证：

```text
python3 -m pytest -q \
  tests/test_gateway.py::NativeGatewayTests::test_failed_responses_tool_output_marks_planner_evidence_error \
  tests/test_gateway.py::NativeGatewayTests::test_responses_function_call_output_becomes_planner_evidence_with_name_and_args \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_output_becomes_planner_evidence_with_string_input \
  tests/test_gateway.py::NativeGatewayTests::test_failed_responses_tool_output_marker_is_not_forwarded_to_final_synthesis \
  tests/test_gateway.py::NativeGatewayTests::test_responses_custom_tool_call_executes_and_appends_custom_output
  5 passed

./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 65 passed
  Agent Planner acceptance gate: PASS

python3 -m pytest -q
  1041 passed, 2 skipped, 21 warnings in 52.90s
```

## 2026-07-05 Tool result cache runtime/tenant 边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

在继续按“云端 Gateway 不是用户本地 workspace”的边界复核 tool result cache 时，发现此前 2026-06-27 的修复只把 resolved workspace 写入 cache key。云端场景下两个认证 downstream client 仍可能声明相同 workspace 字符串；对 `WebFetch` / `WebSearch` / `JsonQuery(data=...)` 这类 Gateway-owned cacheable 工具，仅按 workspace + tool arguments 复用结果仍不够安全。

当前行为：

- cacheable tool 执行前会在内部 cache arguments 追加两个 sentinel：
  - `__gateway_workspace_cache_key = str(_workspace_root())`
  - `__gateway_runtime_cache_key = _runtime_scope_key()`
- runtime scope key 包含当前请求的 authenticated downstream client / tenant / session / workspace 归属；body 里可伪造的 scope 字段不能让另一个 client 复用同一缓存结果。
- sentinel 只参与 `ToolResultCache` key 计算；不会传入实际 tool handler，也不会改变工具对下游可见的参数。
- 持久化 tool cache 继续使用 `ToolResultCache._make_key(tool_name, arguments)` 的 hash；由于 hash 输入已包含 runtime sentinel，DB cache entry 也随请求 runtime scope 隔离。

Focused 验证：

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
  1018 passed, 2 skipped, 21 warnings in 54.09s
git diff --check
  clean
```

## 2026-07-05 JsonQuery file_path 云端 workspace 边界

状态：已修复并完成 focused / acceptance / full pytest 验证。

`JsonQuery` 有两种形态：`data` 入参是纯 Gateway 数据查询，`file_path` / `path` 入参会读取 workspace JSON 文件。此前工具 catalog 将它整体标为 `pure`，导致 public direct endpoint 可以在云端 Gateway 进程里执行 `JsonQuery(file_path=...)`。这不符合云端边界：带文件路径的 JsonQuery 本质是 downstream workspace 读取，不能把 Gateway 服务端 filesystem 当用户项目。

当前行为：

- `JsonQuery(data=..., query=...)` 仍作为 Gateway-owned 纯工具执行。
- `JsonQuery(file_path=.../path=...)` 在默认云端模式下视为 downstream-owned，direct public endpoint 返回 `direct_user_side_tool_requires_downstream_client`，并且嵌套在 `multi_tool_use.parallel` 中也会被同样阻断。
- 显式 local-proxy 兼容模式 `execute_user_side_tools_in_gateway=true` 仍可让 Gateway 本地执行 workspace 文件读取。

Focused 验证：

```text
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_direct_user_side_tool_call_requires_downstream_client_by_default \
  tests/test_gateway.py::NativeGatewayTests::test_more_tool_compat_tree_json_symbols_and_catalog \
  tests/test_gateway.py::NativeGatewayTests::test_multi_tool_use_parallel_executes_nested_gateway_tools \
  tests/test_gateway.py::NativeGatewayTests::test_more_top_tool_aliases_mcp_memory_and_parallel_shapes -q
  4 passed

python3 -m compileall -q src tests
./scripts/agent_planner_acceptance.sh
  focused pytest inside gate: 33 passed
  Agent Planner acceptance gate: PASS
python3 -m pytest -q
  1017 passed, 2 skipped, 21 warnings in 52.49s
git diff --check
  clean
```
