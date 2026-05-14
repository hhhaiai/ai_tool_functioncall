# Hybrid Tool Call Gateway：客户端工具 + Gateway 工具的真实编排方案

## 1. 目标

本方案用于实现一套稳定、真实、非 prompt-fake 的 API tools / function-call 中间层。

核心目标：

1. 用户继续使用 Codex / OpenCode / Claude Code / DeepSeek-TUI 等客户端。
2. 这些客户端已有的本地工具能力仍由客户端自己执行，例如读取文件、搜索代码、执行 shell、编辑文件、apply_patch、子代理等。
3. Gateway 作为用户配置的 API endpoint，对外兼容：
   - `/v1/chat/completions`
   - `/v1/responses`
   - `/v1/messages`
4. Gateway 可以额外声明和执行自己拥有的 tools，例如业务 API、内部知识库、私有 MCP、订单系统、数据库查询等。
5. 如果上游 AI 返回 Gateway 拥有的真实协议级 tool call，Gateway 执行工具，把结果按标准 tool result 协议回填给上游 AI，再继续请求，直到得到最终回复。
6. 如果上游 AI 返回客户端拥有的 tool call，Gateway 不抢执行，直接返回给客户端，由 Codex / OpenCode / Claude Code / DeepSeek-TUI 自己执行。
7. 不解析普通文本里的 JSON / XML / `<function_calls>` 来伪造 tool call。

一句话：

> 客户端工具归客户端执行；Gateway 工具归 Gateway 执行；所有调用都必须基于协议级 `tool_calls` / `function_call` / `tool_use`，不能靠 prompt 伪造。

---

## 2. 为什么不能让 Gateway 执行所有 tools

Codex / Claude Code / OpenCode / DeepSeek-TUI 的本地工具不是普通函数，它们绑定了客户端 runtime：

- 当前工作目录
- 文件系统权限
- shell sandbox
- 用户审批策略
- diff/apply_patch 语义
- UI 交互
- 子代理生命周期
- MCP 连接池
- 本地上下文压缩/恢复逻辑

因此 Gateway 不应该抢执行这些工具：

```text
Read / Write / Edit / Bash / Glob / Grep / apply_patch
exec_command / write_stdin / shell_command
spawn_agent / wait_agent / close_agent
request_user_input
客户端侧 mcp__server__tool
```

这些工具由客户端执行才稳定。Gateway 的职责是补充服务端工具能力，而不是替代客户端 agent runtime。

---

## 3. 总体架构

```text
┌────────────────────────────────────────────────────────────┐
│ Codex / OpenCode / Claude Code / DeepSeek-TUI               │
│                                                            │
│ Client-owned tools:                                         │
│ - read/search/edit/shell/apply_patch                        │
│ - local MCP tools                                           │
│ - subagent / user input / todo / plan                       │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               │ OpenAI / Anthropic compatible request
                               │ messages + client tools + prior tool results
                               ▼
┌────────────────────────────────────────────────────────────┐
│ Hybrid Tool Call Gateway                                    │
│                                                            │
│ 1. Protocol Adapter                                         │
│    - Chat Completions                                       │
│    - Responses                                              │
│    - Anthropic Messages                                     │
│                                                            │
│ 2. Tool Ownership Registry                                  │
│    - client-owned tools                                     │
│    - gateway-owned tools                                    │
│    - provider-owned tools                                   │
│    - unknown tools policy                                   │
│                                                            │
│ 3. Gateway Tool Executor                                    │
│    - internal business tools                                │
│    - server-side MCP tools                                  │
│    - HTTP/function services                                 │
│                                                            │
│ 4. Tool Orchestration Loop                                  │
│    - detect real protocol tool calls                        │
│    - execute only gateway-owned calls                       │
│    - append tool results                                    │
│    - call upstream again                                    │
│                                                            │
│ 5. Safety / Audit                                           │
│    - max rounds                                             │
│    - timeout                                                │
│    - allowlist                                              │
│    - structured logs                                        │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│ Upstream AI Provider                                        │
│ - real native tools/function-call capable model             │
│ - or OpenAI-compatible / Anthropic-compatible upstream       │
└────────────────────────────────────────────────────────────┘
```

---

## 4. Tool Ownership Registry

Gateway 必须维护工具所有权，不允许只按名字盲目执行。

### 4.1 ownership 类型

| owner | 含义 | 执行方 |
|---|---|---|
| `client` | Codex / Claude Code / OpenCode / DeepSeek-TUI 本地工具 | 客户端 |
| `gateway` | Gateway 注册的业务工具、服务端 MCP、内部 API | Gateway |
| `provider` | 上游 provider 自带工具，例如 provider web/file search | 上游 provider |
| `unknown` | 没有注册或无法判断归属 | 按策略处理 |

### 4.2 示例配置

```json
{
  "tools": {
    "mode": "hybrid_auto",
    "max_tool_rounds": 5,
    "unknown_tool_policy": "return_to_client",
    "client_owned_patterns": [
      "Read",
      "Write",
      "Edit",
      "Bash",
      "Glob",
      "Grep",
      "apply_patch",
      "exec_command",
      "write_stdin",
      "shell_command",
      "spawn_agent",
      "wait_agent",
      "close_agent",
      "mcp__*"
    ],
    "gateway_tools": [
      {
        "name": "query_internal_docs",
        "description": "查询内部知识库",
        "executor": "http",
        "endpoint": "https://internal.example.local/search"
      },
      {
        "name": "get_order_status",
        "description": "查询订单状态",
        "executor": "function_service",
        "service": "orders"
      }
    ]
  }
}
```

### 4.3 判断优先级

```text
1. 显式 gateway tool allowlist 命中 -> gateway
2. 显式 client tool allowlist / pattern 命中 -> client
3. provider 内置工具类型命中 -> provider
4. 其他 -> unknown_tool_policy
```

建议默认：

```text
unknown_tool_policy = return_to_client
```

这样对 Codex / Claude Code 最安全，不会误执行客户端工具。

---

## 5. Gateway tools 从哪里来

这里要区分 **tool/function-call 协议** 和 **真实工具实现**：

```text
function call / tool call = 模型请求调用某个工具的协议格式
真实工具实现 = Gateway 收到调用后实际执行的代码、HTTP 服务、MCP server、业务系统等
```

所以不存在“只要支持 function call 就自动拥有所有工具”的情况。Gateway 必须有一个真实可执行的 Tool Registry。

### 5.1 三类工具来源

| 来源 | 是否需要逐一实现 | 说明 | 推荐程度 |
|---|---:|---|---|
| Gateway 内置业务工具 | 需要 | 例如 `gateway__query_internal_docs`、`gateway__get_order_status`，由我们按业务逐个实现 | 必须有 |
| MCP server / MCP registry | 不一定 | Gateway 作为 MCP client，动态读取 `tools/list`，执行 `tools/call`；这是最接近“工具市场”的方式 | 推荐 |
| OpenAPI / HTTP connector | 半自动 | 读取 OpenAPI schema 生成 tool schema，执行时转成 HTTP 请求；鉴权、参数、安全仍要配置 | 推荐 |
| 第三方 workflow/iPaaS | 不一定 | 例如外部已经封装好的 Slack、GitHub、Notion、数据库、CRM 等 action 服务；Gateway 只做连接器适配 | 可选 |
| Provider 内置工具 | 不实现 | 例如上游模型自带 web/search/file_search 之类，由 provider 自己执行 | 透传/声明能力 |
| 客户端本地工具 | 不实现 | Codex/Claude Code/OpenCode/DeepSeek-TUI 的读写文件、shell、patch、agent 等 | 必须返回客户端 |

### 5.2 推荐实现策略

不要一开始把所有 tool 都手写到 Gateway 里。稳定做法是实现一个统一执行接口，然后接多个来源：

```text
Gateway Tool Registry
  ├─ built_in        # 少量安全内置工具：echo/calculator/time
  ├─ business_http   # 业务 HTTP/API 工具
  ├─ openapi         # OpenAPI -> tool schema -> HTTP call
  ├─ mcp             # MCP tools/list + tools/call
  └─ external_action # 第三方 action/workflow 服务
```

统一内部接口：

```python
class GatewayToolExecutor:
    def list_tools(self) -> list[ToolDefinition]: ...
    def can_execute(self, name: str) -> bool: ...
    def execute(self, name: str, arguments: dict) -> ToolResult: ...
```

这样 Gateway 不需要为每个工具写死协议逻辑，只需要为每类工具来源写 adapter。

### 5.3 什么需要逐一实现

需要逐一实现的主要是：

```text
1. 你的核心业务工具
   - 查内部文档
   - 查订单
   - 查用户/权限
   - 查数据库
   - 调内部服务

2. 安全策略
   - 哪些参数允许
   - 哪些用户可调用
   - timeout
   - rate limit
   - audit log

3. 返回结果格式
   - 成功输出
   - 失败输出
   - 大结果截断/摘要
```

不建议逐一实现的：

```text
1. 客户端本地工具：Read/Bash/Edit/apply_patch/agent
2. 通用 SaaS 工具全集：Slack/GitHub/Notion/Jira 等，优先用 MCP/OpenAPI/第三方 action 服务接入
3. Provider 已经内置执行的工具
```

### 5.4 “工具市场”的现实形态

更准确的说法不是 function-call 市场，而是 **工具连接器市场 / MCP server 市场 / action marketplace**。

Gateway 可以支持这类市场，但接入后仍要做：

```text
安装/启用 connector
  -> 拉取工具 schema
  -> 映射成 Chat/Responses/Messages 的 tools 格式
  -> 标记 owner=gateway
  -> 执行时调用对应 MCP/HTTP/action 服务
  -> 把结果转成 tool_result/function_call_output
```

所以最终稳定路线是：

```text
少量核心工具逐一实现
+ MCP/OpenAPI/action adapter 承接大量外部工具
+ ownership registry 防止误执行客户端工具
```

---

## 6. Gateway 工作模式

### 5.1 `passthrough`

纯透传模式。

```text
请求 -> 上游 AI -> 响应 -> 客户端
```

Gateway 不执行任何 tool。

适合：

- 最小风险
- 只想验证 API 是否支持 native tools
- Claude Code / Codex 完全自行执行工具

### 5.2 `gateway_orchestrated`

Gateway 只执行 gateway-owned tools。

```text
请求 -> 上游 AI -> gateway tool_call -> Gateway 执行 -> 回填 -> 上游 AI -> 最终回答
```

如果遇到 client-owned tool call，直接返回客户端。

### 5.3 `hybrid_auto`（推荐默认）

自动判断每个 tool call 的 owner：

```text
gateway-owned tool_call -> Gateway 执行并继续循环
client-owned tool_call  -> 返回客户端执行
provider-owned tool     -> 由 provider 自己处理
unknown tool            -> 按 unknown_tool_policy
```

MVP 建议使用保守策略：

> 只要一轮返回里包含 client-owned tool call，就整轮返回客户端；只有当这一轮全部是 gateway-owned tool calls 时，Gateway 才内部执行并继续请求上游。

这样不会破坏客户端的本地工具链。

---

## 7. 核心执行循环

### 6.1 伪代码

```python
def handle_request(path, body):
    state = normalize_request(path, body)

    for round_index in range(max_tool_rounds):
        upstream_response = call_upstream(path, state.body)
        tool_calls = extract_protocol_tool_calls(path, upstream_response)

        if not tool_calls:
            return upstream_response

        ownerships = classify_tool_calls(tool_calls)

        if has_client_owned_call(ownerships):
            # 不抢客户端工具，直接返回给 Codex / Claude Code / DeepSeek-TUI
            return upstream_response

        if has_unknown_call(ownerships):
            if unknown_tool_policy == "return_to_client":
                return upstream_response
            if unknown_tool_policy == "tool_error":
                tool_results = build_unknown_tool_errors(tool_calls)
            else:
                raise GatewayError("unknown tool call")
        else:
            tool_results = execute_gateway_tools(tool_calls)

        state.body = append_tool_results(path, state.body, upstream_response, tool_results)

    raise GatewayError("max tool rounds exceeded")
```

### 6.2 稳定性要求

- `max_tool_rounds` 必须存在，默认 5。
- 每个工具必须有 timeout。
- 每个工具必须有 allowlist。
- 工具执行失败不要让 HTTP 直接 500，应该生成协议级 tool error result，让模型有机会修正。
- 只有网关内部 bug / 上游不可用 / 协议破损才返回 gateway error。
- 不支持 streaming orchestration 时必须显式拒绝或降级为 non-stream，不要伪造 SSE tool events。

---

## 8. 三种协议的 tool result 回填

## 8.1 `/v1/chat/completions`

### 上游返回真实 tool call

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_1",
            "type": "function",
            "function": {
              "name": "query_internal_docs",
              "arguments": "{\"query\":\"gateway tools\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

### Gateway 追加上下文

```json
[
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [
      {
        "id": "call_1",
        "type": "function",
        "function": {
          "name": "query_internal_docs",
          "arguments": "{\"query\":\"gateway tools\"}"
        }
      }
    ]
  },
  {
    "role": "tool",
    "tool_call_id": "call_1",
    "content": "{\"results\":[...]}"
  }
]
```

然后用更新后的 `messages` 再请求上游。

---

## 8.2 `/v1/responses`

### 上游返回真实 function call

```json
{
  "output": [
    {
      "type": "function_call",
      "call_id": "call_1",
      "name": "query_internal_docs",
      "arguments": "{\"query\":\"gateway tools\"}"
    }
  ]
}
```

### Gateway 追加 input item

```json
{
  "type": "function_call_output",
  "call_id": "call_1",
  "output": "{\"results\":[...]}"
}
```

实现时需要保留上一轮 `function_call` 上下文。推荐 MVP 不依赖 `previous_response_id`，而是把历史 output/input 合并成可重放 input，避免不同 OpenAI-compatible provider 对 `previous_response_id` 支持不一致。

---

## 8.3 `/v1/messages`

### 上游返回真实 tool_use

```json
{
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_1",
      "name": "query_internal_docs",
      "input": {
        "query": "gateway tools"
      }
    }
  ],
  "stop_reason": "tool_use"
}
```

### Gateway 追加 messages

```json
[
  {
    "role": "assistant",
    "content": [
      {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "query_internal_docs",
        "input": {
          "query": "gateway tools"
        }
      }
    ]
  },
  {
    "role": "user",
    "content": [
      {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "{\"results\":[...]}"
      }
    ]
  }
]
```

然后再请求上游 `/v1/messages`。

---

## 9. 请求里的 tools 如何处理

客户端发来的请求可能已经带有 tools。Gateway 需要合并 tools，但不能破坏客户端工具定义。

### 9.1 输入

```json
{
  "tools": [
    {"type": "function", "function": {"name": "Read", "parameters": {...}}},
    {"type": "function", "function": {"name": "Bash", "parameters": {...}}}
  ]
}
```

### 9.2 Gateway 合并自己的 tools

```json
{
  "tools": [
    {"type": "function", "function": {"name": "Read", "parameters": {...}}},
    {"type": "function", "function": {"name": "Bash", "parameters": {...}}},
    {
      "type": "function",
      "function": {
        "name": "query_internal_docs",
        "description": "查询内部知识库",
        "parameters": {...}
      }
    }
  ]
}
```

### 9.3 冲突规则

如果客户端工具和 Gateway 工具同名：

```text
默认：客户端优先，Gateway 不覆盖。
```

原因：Codex / Claude Code 这类客户端对工具名有强语义，覆盖会破坏它们的执行链。

Gateway 工具建议使用明确前缀：

```text
gateway__query_internal_docs
gateway__get_order_status
internal__search_docs
biz__query_order
```

---

## 10. curl 行为示例

## 10.1 Chat Completions：Gateway 工具被内部执行

客户端请求：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{
    "model": "your-model",
    "messages": [
      {"role":"user","content":"查一下内部文档里 gateway tools 的设计，然后总结"}
    ],
    "tools": []
  }'
```

Gateway 转发给上游时会合并 `gateway__query_internal_docs`。如果上游返回该 tool call，Gateway 内部执行并继续请求，最终客户端只拿到最终 assistant answer。

## 10.2 Chat Completions：客户端工具返回客户端执行

如果上游返回：

```json
{
  "tool_calls": [
    {
      "id": "call_read",
      "type": "function",
      "function": {
        "name": "Read",
        "arguments": "{\"file_path\":\"README.md\"}"
      }
    }
  ]
}
```

Gateway 判断 `Read = client`，不执行，原样返回给 Codex / Claude Code / DeepSeek-TUI。

---

## 11. 错误处理

### 11.1 Gateway tool 执行失败

不要直接让整个请求 500。应该生成 tool error result：

Chat Completions：

```json
{
  "role": "tool",
  "tool_call_id": "call_1",
  "content": "ToolExecutionError: internal docs service timeout"
}
```

Anthropic Messages：

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_1",
  "content": "ToolExecutionError: internal docs service timeout",
  "is_error": true
}
```

Responses：

```json
{
  "type": "function_call_output",
  "call_id": "call_1",
  "output": "ToolExecutionError: internal docs service timeout"
}
```

### 11.2 上游不返回协议级 tool call

如果只是普通文本，不做工具解析。

```text
普通文本 JSON != tool_call
<function_calls>...</function_calls> != tool_call
markdown 代码块 JSON != tool_call
```

### 11.3 max rounds 超限

返回 gateway error：

```json
{
  "error": {
    "message": "max tool rounds exceeded",
    "type": "tool_orchestration_error",
    "fake_prompt_tools": false
  }
}
```

---

## 12. MVP 实现范围

第一阶段建议只做非流式。

### 12.1 必须实现

1. `GATEWAY_TOOL_MODE=hybrid_auto|passthrough|gateway_orchestrated`
2. `GATEWAY_MAX_TOOL_ROUNDS=5`
3. Tool ownership registry
4. Gateway tool registry
5. 三个协议的 tool call 提取：
   - Chat: `choices[].message.tool_calls`
   - Responses: `output[].type == function_call`
   - Messages: `content[].type == tool_use`
6. 三个协议的 tool result 追加。
7. 只执行 gateway-owned tools。
8. client-owned tools 原样返回客户端。
9. unknown tool 默认返回客户端。
10. 不解析文本伪 function call。

### 12.2 第一批 Gateway tools

建议只内置安全工具：

```text
gateway__echo_probe
gateway__calculator
gateway__get_current_time
```

后续再接：

```text
gateway__query_internal_docs
gateway__call_mcp_tool
gateway__http_request_allowlisted
```

### 12.3 暂不实现

```text
streaming orchestration
Gateway 执行 shell
Gateway 写文件
Gateway 替代 Codex/Claude Code apply_patch
Gateway 执行客户端 MCP tools
```

---

## 13. 测试清单

### 13.1 Chat Completions

- 上游第一轮返回 gateway-owned tool call。
- Gateway 执行工具。
- 第二轮请求包含 assistant `tool_calls` + `role:tool`。
- 最终返回 assistant answer。

### 13.2 Responses

- 上游第一轮返回 `function_call`。
- Gateway 执行工具。
- 第二轮 input 包含 `function_call_output`。
- 最终返回 message output。

### 13.3 Anthropic Messages

- 上游第一轮返回 `tool_use`。
- Gateway 执行工具。
- 第二轮 messages 包含 assistant `tool_use` + user `tool_result`。
- 最终返回 text content。

### 13.4 客户端工具不被抢执行

- `Read` 返回客户端。
- `Bash` 返回客户端。
- `apply_patch` 返回客户端。
- `mcp__server__tool` 返回客户端。

### 13.5 防 fake

- 普通文本 JSON 不触发执行。
- markdown 代码块 JSON 不触发执行。
- `<function_calls>` 不触发执行。

### 13.6 稳定性

- 工具 timeout。
- max rounds。
- unknown tool policy。
- tool 执行失败转成 tool error result。
- 同名工具冲突时客户端优先。

---

## 14. 最终稳定原则

1. **客户端 runtime 不替代**：Codex / Claude Code / OpenCode / DeepSeek-TUI 的本地工具继续由它们自己执行。
2. **Gateway 只执行自己拥有的 tools**：必须在 registry 里显式注册。
3. **协议级才算 tool call**：只认 `tool_calls` / `function_call` / `tool_use`。
4. **不 fake**：不从 prompt 文本、JSON 文本、XML 文本里伪造工具调用。
5. **混合轮保守返回客户端**：一轮里只要有 client-owned tool call，就优先返回客户端。
6. **可观测**：每次 tool call 记录 owner、round、tool name、duration、success/error。
7. **可控**：allowlist、timeout、max rounds、unknown policy 必须可配置。
8. **非流式先稳定**：streaming tool orchestration 后续单独实现，不要先做半套。
