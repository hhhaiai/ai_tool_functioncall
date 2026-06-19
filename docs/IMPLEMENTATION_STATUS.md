# Gateway 实现状态文档

> 最后更新: 2026-06-19

## 2026-06-19 收敛状态

本轮按最终 Gateway 目标做全仓库回归：上游可为不支持 tools/function calls 的普通 API；下游面向 Claude Code / Codex；Gateway 负责协议适配、必要的文本工具适配、workspace 隔离、记忆/上下文治理、gateway-owned 工具执行，以及用户侧工具的协议级下发。

关键收敛点：
- 普通无 tools 请求不再自动注入大段工具 adapter，避免污染上游普通对话。
- 请求级 workspace 不再默认回退 Gateway 服务 cwd；缺失 workspace 时使用匿名隔离空间，显式 env/config root 仅作兜底。
- 工具归属已明确：HTTP Action/MCP/WebFetch/WebSearch/calculator/Memory 等 gateway-owned 工具由 Gateway 真执行；Read/LS/Glob/Grep/Write/Edit/Bash/Skill/GUI/local agent 等用户机器工具默认返回下游原生 tool request，由 Claude Code/Codex 在用户机器执行。
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
- Gateway-owned（Gateway 真执行）: `echo_probe`, `calculator`, `get_current_time`, HTTP Actions（如天气/内部 API）, MCP tools/resources, `WebFetch`, `WebSearch`, `Memory`, 纯函数/状态类工具。
- User-side（默认下发给 Claude Code/Codex 执行）: `Read`, `ReadManyFiles`, `FileInfo`, `LS`, `Tree`, `Glob`, `Grep`, `Write`, `Edit`, `Bash`, `Git`, `PythonSymbols`, `Skill`, GUI/computer_use/click/type 等依赖用户机器/项目的工具。
- 兼容旧本地代理部署：显式设置 `gateway.execute_user_side_tools_in_gateway=true`（或 legacy `delegate_tools_to_downstream=false`）后，才允许用户侧工具在 Gateway 服务机执行。

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
