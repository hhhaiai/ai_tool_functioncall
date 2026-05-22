# Gateway 架构文档

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
└── gateway_http_handler.py   # HTTP 入口处理
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
        ▼
┌───────────────────┐
│ gateway_protocol  │  ← 转换为上游格式
└───────────────────┘
        │
        ▼
┌───────────────────┐
│ gateway_proxy     │  ← 向上游发起请求
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
      "supports_tools": true,
      "supports_function_calls": true,
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

## 8. 下游兼容

| 下游客户端 | 协议 | 接入方式 |
|------------|------|----------|
| Claude Code | Anthropic Messages | `ANTHROPIC_BASE_URL` |
| Codex | OpenAI Chat | `OPENAI_BASE_URL` |
| DeepSeek-TUI | OpenAI Chat | `OPENAI_BASE_URL` |
| OpenCode | OpenAI Chat | `OPENAI_BASE_URL` |
| Python SDK | OpenAI Chat/Responses | `base_url + api_key` |

所有下游通过 Gateway 后，都获得一致的 Tool Call 使用入口；真实执行能力来自 Gateway 内置工具、MCP、HTTP Action、provider-native tools 或其它明确配置的 executor。