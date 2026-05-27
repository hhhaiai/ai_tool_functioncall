# Gateway 架构文档

> 真实测试上游 / Mimo 作为上游时默认按 **Gateway adapter** 处理：`tools_enabled=adapter`，`supports_tools=false`，`supports_function_calls=false`。真实地址只放本地 `.gateway_service.json`、`.env` 或运行时环境变量，不写入提交代码。当前探针显示 `/v1/messages` 在 forced tool_choice 下可返回 Anthropic `tool_use`，但上游直连没有 `/anthropic` 别名、没有 `/v1/tools/call` / `/v1/functions/call`，且 `/v1/responses` forced tool probe 未返回 Codex 需要的 `function_call`。因此 Claude Code/Codex 不直连该上游执行工具，而是连接 Gateway，由 Gateway 本地 runtime/MCP/HTTP Actions 执行真实工具，再把结果回填给上游模型继续生成。Mimo 上下文按 `1048576` tokens（1M）配置。

## 1. 系统定位

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  上游                                        │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                       │
│   │  Chat API   │  │  Sub API    │  │  Full API   │                       │
│   │ (不支持tool) │  │ (部分支持)  │  │ (完全支持)  │                       │
│   └─────────────┘  └─────────────┘  └─────────────┘                       │
│         │                │                │                               │
└─────────┼────────────────┼────────────────┼───────────────────────────────┘
          │                │                │
          ▼                ▼                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  中游  Gateway                              │
│                                                                             │
│   ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐              │
│   │ 协议转换  │  │ 工具桥接   │  │ 上下文管理 │  │ 配置管理  │              │
│   │ OpenAI ↔ │  │ 补齐缺失   │  │ 压缩/记忆  │  │ Web UI   │              │
│   │ Claude   │  │ 工具能力   │  │ 无限上下文 │  │          │              │
│   └───────────┘  └───────────┘  └───────────┘  └───────────┘              │
└─────────────────────────────────────────────────────────────────────────────┘
          │                │                │                │
          ▼                ▼                ▼                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  下游                                        │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│   │ Claude Code │  │   Codex     │  │ DeepSeek   │  │  OpenCode   │     │
│   └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 2. 上游类型

| 类型 | Tool 支持 | 代表厂商 | Gateway 处理方式 |
|------|-----------|----------|------------------|
| **Chat API** | ❌ 不支持 | 某些三方模型 | 文本注入 + 本地工具执行 |
| **Sub API** | ⚠️ 部分支持 | 部分商家的精简版 | 补齐缺失能力 |
| **Full API** | ✅ 完全支持 | OpenAI、Claude 官方 | 直接透传 |

## 3. 中游作用

### 3.1 协议转换

```
下游请求格式          →           转换           →          上游格式
─────────────────────────────────────────────────────────────────────
OpenAI Chat          →  gateway_protocol  →  OpenAI Chat
OpenAI Responses     →  gateway_protocol  →  OpenAI Responses
Anthropic Messages   →  gateway_protocol  →  Anthropic Messages

上游响应格式          →           转换           →          下游格式
─────────────────────────────────────────────────────────────────────
OpenAI Chat          →  gateway_protocol  →  任意下游格式
OpenAI Responses     →  gateway_protocol  →  任意下游格式
Anthropic Messages   →  gateway_protocol  →  任意下游格式
```

**核心模块**: `gateway_protocol.py`

### 3.2 工具桥接

对于上游不支持或部分支持工具的场景：

```
上游返回 (无tool能力)          Gateway 本地执行
         │                           │
         ▼                           ▼
┌──────────────────┐      ┌────────────────────────┐
│ 文本协议提示      │      │ gateway_tool_runtime   │
│ 解析弱工具调用    │      │ 真实执行工具 + 回填结果 │
└──────────────────┘      └────────────────────────┘
```

**核心模块**: `gateway_tool_runtime.py`, `gateway_builtin_tools.py`

Gateway 是中游服务，不把自身启动目录当作用户项目目录。每个请求会先解析当前下游项目根：请求体 `workspace_root` / `gateway_workspace` 最优先，其次是 Claude Code 的 `Primary working directory` / `Worktree`、Codex Responses 的 `<environment_context><cwd>`、metadata 中的 `projectDir` / `cwd`，最后才回退到 env/config/default。工具读写、项目级 `.traces`、SQLite 记忆隔离、`Skill` 工具和项目插件都使用该请求级项目根。

`Skill` / `list_skills` / `read_skill` / `run_skill` 是真实 Gateway 工具。项目内 `.codex/skills`、`.claude/skills`、`.opencode/skills`、`.agents/skills`、`skills/` 优先；项目内 `.codex/plugins` / `.claude/plugins` / `.opencode/plugins` / `plugins` 的 manifest 可声明 skills，但声明路径必须仍在项目根内；用户全局 skills 与 `GATEWAY_SKILLS_DIRS` 在项目目录之后加载。

### 3.3 上下文管理（无限上下文）

```
┌────────────────────────────────────────────────────────────────┐
│                     Context Management                         │
│                                                                │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐ │
│  │  入口    │ → │ Token    │ → │  压缩    │ → │  扇出    │ │
│  │ Request  │   │ 估算     │   │ 摘要      │   │  并行    │ │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘ │
│                                            │                 │
│                  ┌─────────────────────────┘                 │
│                  ▼                                           │
│           ┌──────────┐                                      │
│           │  记忆    │  ← SQLite 持久化                      │
│           │ Memory   │                                      │
│           └──────────┘                                      │
└────────────────────────────────────────────────────────────────┘
```

**核心模块**: `gateway_context.py`

### 3.4 配置管理

- **Web UI**: `gateway_admin.py` + `gateway_http_handler.py`
- **配置文件**: `gateway_config.py`
- 支持配置上游能力、模型参数、工具权限等

## 4. 模块架构

```
src/
├── gateway_app.py              # 入口导出
├── gateway_config.py          # 配置管理
│   ├── 上游 profiles（多个上游配置）
│   ├── 下游 keys
│   └── Gateway 自身配置
├── gateway_protocol.py        # 协议转换 ⭐
│   ├── OpenAI Chat ↔ Anthropic Messages
│   ├── OpenAI Chat ↔ OpenAI Responses
│   └── 工具格式互转
├── gateway_proxy.py           # 上游 HTTP 客户端
├── gateway_context.py         # 上下文压缩/记忆 ⭐
│   ├── Token 估算
│   ├── 消息压缩/摘要
│   ├── 扇出（Fanout）并行处理
│   └── 记忆系统（SQLite）
├── gateway_tool_runtime.py   # 工具执行引擎 ⭐
│   ├── 工具解析/规范化
│   ├── 执行编排（Orchestration）
│   └── 内置工具调度
├── gateway_builtin_tools.py   # 内置工具实现
│   ├── 文件操作（Read/Write/Edit/Glob/Grep）
│   ├── Shell 执行（Bash/Shell）
│   ├── 网络（WebFetch/WebSearch）
│   └── Agent（TodoWrite/ExitPlanMode）
├── gateway_streaming.py       # SSE 流式处理
├── gateway_mcp.py             # MCP 协议支持
├── gateway_http_actions.py    # HTTP Action 支持
├── gateway_admin.py           # Admin UI 渲染
├── gateway_http_handler.py    # HTTP 入口处理
├── gateway_cache.py           # 语义缓存 ⭐
│   ├── LocalEmbeddingProvider (字符 trigram + 词频)
│   ├── SemanticCache (余弦相似度匹配)
│   └── ToolResultCache (确定性工具缓存)
├── gateway_intelligence.py    # 智力提升 ⭐
│   ├── 问题分析 (复杂度/领域/工具需求)
│   ├── 反思机制 (反思提示生成)
│   └── 质量评估 (完整性/相关性/清晰度)
├── gateway_stats.py           # Q&A 统计 ⭐
│   ├── 请求/工具/缓存/质量统计
│   ├── SQLite 持久化
│   └── 仪表板/趋势/导出
├── gateway_concurrency.py     # 并发优化
│   ├── ConnectionPool (连接池)
│   ├── LoadBalancer (负载均衡)
│   └── MultiUpstreamManager (多上游管理)
├── gateway_web2api.py         # Web → API 引擎
│   ├── CSS 选择器提取
│   ├── 正则提取
│   └── 自动元数据提取
├── gateway_web_config.py      # Web 配置 UI
│   ├── Tab 式配置界面 (9 个标签页)
│   └── 配置 Schema + 更新 API
└── gateway_claude_compat.py   # Claude Code 兼容层
    ├── 工具定义 (Read/Write/Edit/Bash/Glob/Grep/WebFetch/WebSearch)
    └── 格式化工具 (tool_result/tool_use)
```

## 5. 请求流程

```
1. 下游请求 (任意格式)
        │
        ▼
┌───────────────────┐
│ gateway_http_handler  │  ← 解析请求、认证
└───────────────────┘
        │
        ├── 智力增强 (gateway_intelligence) ← 分析问题复杂度/领域，注入增强 system prompt
        ├── 语义缓存检查 (gateway_cache)    ← 精确/相似匹配，命中则直接返回
        │
        ▼
┌─────────────────────┐
│ request workspace    │  ← 解析下游项目根，隔离工具/Skills/.traces/记忆
└─────────────────────┘
        │
        ▼
┌───────────────────┐
│ gateway_protocol  │  ← 转换为上游格式
└───────────────────┘
        │
        ▼
┌───────────────────┐
│ gateway_proxy     │  ← 向上游发起请求（含重试）
└───────────────────┘
        │
   ┌────┴────┐
   │ 上游响应  │
   └────┬────┘
        ▼
┌───────────────────┐
│ gateway_tool_runtime │  ← 检测工具调用
└───────────────────┘
        │
   ┌────┴────┐
   │ 有工具调用 │  ──→ 执行工具 → 返回结果
   └────┬────┘
        │
        ▼
┌───────────────────┐
│ gateway_protocol  │  ← 转换响应格式
└───────────────────┘
        │
        ▼
┌───────────────────┐
│ 返回下游 (任意格式) │
└───────────────────┘
```

## 6. 上下文压缩流程

```
用户请求
    │
    ▼
Token 估算 (body_token_estimate)
    │
    ▼
超限？ ──→ 摘要压缩 (compact_messages_with_summary)
    │
    │  记忆召回 (recall_conversation_memories)
    │
    ▼
注入摘要 + 记忆上下文
    │
    ▼
发送给上游
```

## 7. 配置项说明

### 上游能力配置

```json
{
  "upstream": {
    "capabilities": {
      "supports_streaming": true,
      "supports_tools": false,
      "supports_function_calls": false,
      "supports_vision": false,
      "supports_network": false
    }
  }
}
```

### 上下文配置

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

### 缓存配置

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

### 智力提升配置

```json
{
  "intelligence": {
    "enabled": true,
    "reflection_enabled": true,
    "quality_threshold": 0.6
  }
}
```

### 统计配置

```json
{
  "stats": {
    "enabled": true,
    "retention_days": 30
  }
}
```

## 8. 下游兼容

| 下游客户端 | 协议 | 接入方式 |
|------------|------|----------|
| Claude Code | Anthropic Messages | `ANTHROPIC_BASE_URL` |
| Codex | OpenAI Chat | `OPENAI_BASE_URL` |
| DeepSeek-TUI | OpenAI Chat | `OPENAI_BASE_URL` |
| OpenCode | OpenAI Chat | `OPENAI_BASE_URL` |
| Python SDK | OpenAI Chat/Responses | `base_url + api_key` |

所有下游通过 Gateway 后，都获得一致的 Tool Call 使用入口；真实执行能力来自 Gateway 内置工具、MCP、HTTP Action、provider-native tools 或其它明确配置的 executor。Claude Code / Codex 的工具、Skills 和项目级路径按“当前下游项目根”隔离，内部 `workspace_root` / `gateway_workspace` / `projectDir` / `cwd` 等路由字段只在 Gateway 内使用，不作为上游模型语义透传；普通转发与 streaming passthrough 都会在上游请求前清理这些字段及 metadata JSON 里的同类嵌套字段。
