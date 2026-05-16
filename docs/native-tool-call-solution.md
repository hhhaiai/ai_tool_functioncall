# 原生级 tools / function-call 支持方案

## 0. 目标

你要的不是“把 tools 写进 prompt，让模型输出 JSON”的假 function call，而是让 Claude Code / Codex / OpenAI SDK / Anthropic SDK 这类客户端真正看到并使用协议级字段：

- OpenAI Chat Completions: `message.tool_calls`, `finish_reason=tool_calls`
- OpenAI Responses: `output[].type=function_call` / `function_call_output`
- Anthropic Messages: `content[].type=tool_use` / `tool_result`
- 对外保持标准 HTTP API，不让客户端感知背后接的是哪个供应商

核心判断：

> 真正原生级 tool call 不能靠 prompt 伪造。必须由上游模型/API 本身返回协议级工具调用对象，或者由一个已经实现原生 tool-call 协议的外部服务返回这些对象。我们的这一层只能做协议适配、校验、路由、编排、鉴权和失败兜底，不能把“不支持原生 tools 的模型”变成“真实原生 tools 模型”。

因此方案必须是 **Native Tool Gateway**，不是 prompt shim。

---

## 1. 能力边界

### 1.1 可以真正实现的

可以实现一个统一网关，对外暴露：

- `/v1/chat/completions`
- `/v1/responses`
- `/v1/messages`

网关内部对接多个已经具备原生能力的后端：

- OpenAI 原生 tools / Responses tools
- Anthropic 原生 `tool_use`
- 其他 OpenAI-compatible 且真实返回 `tool_calls` 的 API
- 外部已经实现 function call 的服务
- 本地/私有模型服务，只要它真实返回协议级 tool call 对象

网关职责：

1. 接收客户端标准 tools/function-call 请求。
2. 按后端协议转换 tools schema。
3. 转发给真实支持 tool call 的后端。
4. 校验后端返回里是否真的存在协议级 tool call 对象。
5. 选择两种模式之一：
   - 透传模式：把 tool call 原样返回给客户端，让 Claude Code / Codex 执行工具。
   - 服务端编排模式：网关自己执行外部工具，再用标准 tool-result 协议回传给模型，循环直到最终回答。
6. 如果后端不支持或没有返回真实 tool call，直接失败，不伪装成功。

### 1.2 文本级工具调用的处理

如果一个上游 API 只支持普通文本对话，完全不支持 native `tools/tool_calls/tool_use`，那么：

- 不能宣称它支持原生 function call。
- 不能用 prompt JSON 冒充 `tool_calls`。
- 不能给 Claude Code / Codex 返回”看起来像工具调用但不是模型原生决策”的结果。

**但是**，Gateway 可以识别模型文本输出中的 Claude-Code-like 标记（如 `<function=Glob>`），执行真实工具，并把结果回填给上游继续生成最终答案。这是真实工具执行，不是伪造。

处理策略：
1. 优先使用原生 tool_calls/tool_use（如果上游支持）
2. 如果原生不支持，解析文本中的工具调用标记
3. 执行真实工具并回填结果
4. 循环直到最终回答

---

## 2. 总体架构

```text
Claude Code / Codex / SDK / 你的应用
        │
        │ 标准 API 请求：tools / tool_choice / tool_result
        ▼
┌────────────────────────────────────────────┐
│ Native Tool Gateway                         │
│                                            │
│  1. Client Protocol Adapter                │
│     - OpenAI Chat Completions              │
│     - OpenAI Responses                     │
│     - Anthropic Messages                   │
│                                            │
│  2. Capability Registry                    │
│     - 哪个 provider 支持 native tools       │
│     - 哪个 endpoint 支持 streaming tools    │
│     - 哪个模型支持 forced tool_choice       │
│                                            │
│  3. Provider Router                        │
│     - 按模型 / endpoint / tool 能力路由      │
│                                            │
│  4. Provider Adapter                       │
│     - OpenAI adapter                       │
│     - Anthropic adapter                    │
│     - OpenAI-compatible adapter            │
│     - External function-call service adapter│
│                                            │
│  5. Native Tool Verifier                   │
│     - 校验返回是否真有 tool_calls/tool_use   │
│     - forced tool call 未返回则失败         │
│                                            │
│  6. Optional Server Tool Orchestrator       │
│     - 执行外部工具                          │
│     - 回写标准 tool result                  │
│     - 多轮循环                              │
└────────────────────────────────────────────┘
        │
        ▼
真实支持原生 tools 的上游模型/API/外部服务
```

---

## 3. 两种工作模式

## 3.1 模式 A：Native Passthrough，给 Claude Code / Codex 用

这是最适合 Claude Code / Codex 的模式。

流程：

```text
客户端发 tools
  ↓
网关转换/转发给真实支持 tools 的模型
  ↓
模型返回原生 tool_calls/tool_use
  ↓
网关校验字段真实存在
  ↓
原样返回给客户端
  ↓
Claude Code / Codex 自己执行工具
  ↓
客户端把 tool result 再发回来
  ↓
网关继续原生透传
```

优点：

- 客户端看到的是标准协议字段。
- Claude Code / Codex 的工具权限、沙箱、执行链路仍由客户端控制。
- 不会把服务端伪造结果混进客户端工具系统。
- 最接近“原生级别真实支持”。

适合：

- Claude Code
- Codex
- OpenAI SDK agents
- Anthropic SDK tool use
- 你自己写的 agent runtime

关键要求：

- 上游必须真实支持 tool call。
- 网关不能删除或改写 `tool_calls/tool_use`。
- forced `tool_choice` 时，返回必须包含工具调用，否则判定失败。

---

## 3.2 模式 B：Server-side Native Tool Orchestration，给普通业务 API 用

这种模式不是伪造工具调用，而是服务端真的执行工具循环；区别是工具不是 Claude Code / Codex 执行，而是网关执行。

流程：

```text
客户端发 tools
  ↓
网关转发给真实 native-tools 模型
  ↓
模型返回真实 tool_calls/tool_use
  ↓
网关执行已注册的外部工具
  ↓
网关用标准 tool result 格式回传给模型
  ↓
模型输出最终回答
  ↓
网关返回最终回答给客户端
```

适合：

- 业务后端希望“一次请求拿最终答案”
- 工具是服务端私有 API，例如订单、数据库、内部系统
- 不希望客户端直接拿到工具调用细节

不适合：

- Claude Code / Codex 的编辑、shell、文件系统工具。它们通常应该由客户端自己控制。

---

## 4. 协议适配设计

## 4.1 内部统一结构

网关内部不要直接用某一家 API 的字段作为核心结构，应该归一化为：

```json
{
  "conversation": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "tools": [
    {
      "name": "calculator",
      "description": "执行计算",
      "input_schema": {
        "type": "object",
        "properties": {
          "expression": {"type": "string"}
        },
        "required": ["expression"]
      }
    }
  ],
  "tool_choice": "auto|required|none|specific_tool",
  "stream": false,
  "mode": "passthrough|server_orchestrated"
}
```

再由 adapter 转成具体协议。

## 4.2 OpenAI Chat Completions adapter

请求工具定义：

```json
{
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "calculator",
        "description": "执行计算",
        "parameters": {
          "type": "object",
          "properties": {
            "expression": {"type": "string"}
          },
          "required": ["expression"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

真实工具调用返回必须是：

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_xxx",
            "type": "function",
            "function": {
              "name": "calculator",
              "arguments": "{\"expression\":\"1+2\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

工具结果回传：

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "{\"result\":3}"
}
```

## 4.3 OpenAI Responses adapter

真实工具调用返回必须包含类似：

```json
{
  "output": [
    {
      "type": "function_call",
      "call_id": "call_xxx",
      "name": "calculator",
      "arguments": "{\"expression\":\"1+2\"}"
    }
  ]
}
```

工具结果回传：

```json
{
  "type": "function_call_output",
  "call_id": "call_xxx",
  "output": "{\"result\":3}"
}
```

## 4.4 Anthropic Messages adapter

请求工具定义：

```json
{
  "tools": [
    {
      "name": "calculator",
      "description": "执行计算",
      "input_schema": {
        "type": "object",
        "properties": {
          "expression": {"type": "string"}
        },
        "required": ["expression"]
      }
    }
  ]
}
```

真实工具调用返回必须是：

```json
{
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_xxx",
      "name": "calculator",
      "input": {"expression": "1+2"}
    }
  ],
  "stop_reason": "tool_use"
}
```

工具结果回传：

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_xxx",
      "content": "{\"result\":3}"
    }
  ]
}
```

---

## 5. Provider 能力注册

需要维护一个 provider registry，不要靠猜。

示例：

```yaml
providers:
  openai:
    base_url: https://api.openai.com
    protocol: openai
    endpoints:
      chat_completions:
        native_tools: true
        forced_tool_choice: true
        streaming_tool_events: true
      responses:
        native_tools: true
        forced_tool_choice: true
        streaming_tool_events: true

  anthropic:
    base_url: https://api.anthropic.com
    protocol: anthropic
    endpoints:
      messages:
        native_tools: true
        forced_tool_choice: true
        streaming_tool_events: true

  some_openai_compatible:
    base_url: https://example.com
    protocol: openai_compatible
    endpoints:
      chat_completions:
        native_tools: unknown
        forced_tool_choice: unknown
        streaming_tool_events: unknown
```

`unknown` 不能直接当 true 用，必须通过 probe 验证后写入缓存。

---

## 6. Native capability probe

每个 provider / model / endpoint 启用前都跑探测。

## 6.1 Chat Completions probe

请求：

```json
{
  "model": "目标模型",
  "messages": [
    {"role": "user", "content": "Call echo_probe with value native_probe."}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "echo_probe",
        "description": "Probe native tool calling.",
        "parameters": {
          "type": "object",
          "properties": {"value": {"type": "string"}},
          "required": ["value"]
        }
      }
    }
  ],
  "tool_choice": {
    "type": "function",
    "function": {"name": "echo_probe"}
  }
}
```

通过条件：

- HTTP 200
- `choices[0].message.tool_calls` 存在
- `tool_calls[0].function.name == echo_probe`
- `arguments` 是合法 JSON 或至少是可解析参数字符串
- `finish_reason == tool_calls` 或等价字段

失败条件：

- 400 unknown field `tools`
- 200 但只返回普通文本
- 200 但参数是自然语言
- forced tool_choice 被忽略

## 6.2 Anthropic Messages probe

通过条件：

- `content[].type == tool_use`
- `name == echo_probe`
- `stop_reason == tool_use`

## 6.3 Responses probe

通过条件：

- `output[].type == function_call`
- `name == echo_probe`
- `call_id` 存在

---

## 7. 路由规则

推荐路由逻辑：

```text
请求包含 tools 吗？
  ├─否 → 可走普通 chat provider
  └─是 → 检查 upstream 是否支持原生 tools
          ├─支持 → 转发 native tools 请求
          │        ├─返回真实 tool call → 透传/编排
          │        ├─返回最终文本 → 如果 tool_choice=auto，可接受
          │        └─forced tool_choice 但无 tool call → 失败
          └─不支持 → 启用文本级工具调用解析
                     ├─解析到 Claude-Code-like 标记 → 执行真实工具并回填
                     └─未解析到 → 返回普通文本回答
```

关键点：

- `tool_choice=auto` 时，模型可以选择不调用工具。
- `tool_choice=required` 或指定工具时，必须返回工具调用，否则失败。
- 当上游不支持原生 tools 时，Gateway 可以解析文本中的工具调用标记并执行真实工具。
- 文本级工具调用解析是 fallback 机制，不是伪造。

---

## 8. Claude Code / Codex 接入方式

## 8.1 给 Codex/OpenAI-compatible 客户端

对外提供 OpenAI-compatible base URL：

```text
http://127.0.0.1:8885/v1
```

客户端调用：

```bash
curl http://127.0.0.1:8885/v1/chat/completions \
  -H 'Authorization: Bearer local-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "native-tools-model",
    "messages": [{"role":"user","content":"帮我算 1+2"}],
    "tools": [
      {
        "type":"function",
        "function":{
          "name":"calculator",
          "description":"计算器",
          "parameters":{
            "type":"object",
            "properties":{"expression":{"type":"string"}},
            "required":["expression"]
          }
        }
      }
    ],
    "tool_choice":"auto"
  }'
```

如果上游真实支持，返回必须保留：

```json
"tool_calls": [...]
```

## 8.2 给 Claude/Anthropic-compatible 客户端

对外提供 Anthropic-compatible `/v1/messages`：

```bash
curl http://127.0.0.1:8885/v1/messages \
  -H 'x-api-key: local-key' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{
    "model":"native-tools-model",
    "max_tokens":1024,
    "messages":[{"role":"user","content":"帮我算 1+2"}],
    "tools":[
      {
        "name":"calculator",
        "description":"计算器",
        "input_schema":{
          "type":"object",
          "properties":{"expression":{"type":"string"}},
          "required":["expression"]
        }
      }
    ]
  }'
```

返回必须保留：

```json
{"type":"tool_use", ...}
```

## 8.3 注意

Claude Code / Codex 的“本地工具能力”通常不只是模型 API 的 function call，还包括：

- 文件读写权限
- shell 执行权限
- patch/edit 工具
- MCP 工具
- 沙箱和审批策略
- tool result 回填

网关能做的是提供模型侧 native tool-call 协议能力；客户端本地工具执行仍应由 Claude Code / Codex 自己负责，除非你明确启用 server-side orchestration。

---

## 9. 外部已实现工具服务的对接

如果你已经有外部 function-call/tool-use 服务，网关应该支持两类 adapter。

### 9.1 Native model provider adapter

外部服务本身像模型一样返回 tool call：

```text
request with tools → response with tool_calls/tool_use
```

这种最简单，网关只做协议转换和校验。

### 9.2 Tool executor adapter

外部服务只是工具执行器：

```text
tool name + args → tool result
```

这种用于 server-side orchestration。

工具注册示例：

```yaml
tools:
  calculator:
    executor: http
    url: http://tool-service.local/calculate
    timeout_ms: 3000
    input_schema:
      type: object
      properties:
        expression:
          type: string
      required: [expression]
```

执行时要求：

- 严格 schema 校验。
- 工具名白名单。
- timeout。
- audit log。
- 不允许模型构造任意 URL 请求。
- 不允许工具结果覆盖 system 指令。

---

## 10. 失败策略

必须 fail-fast，不能 fake。

### 10.1 上游不认识 tools

返回：

```json
{
  "error": {
    "type": "native_tools_not_supported",
    "message": "upstream rejected tools/tool_choice; no prompt fallback was used",
    "fake_prompt_tools": false
  }
}
```

### 10.2 forced tool_choice 被忽略

返回：

```json
{
  "error": {
    "type": "native_tool_verification_failed",
    "message": "forced tool_choice did not produce protocol-level tool call",
    "fake_prompt_tools": false
  }
}
```

### 10.3 provider 未探测

返回：

```json
{
  "error": {
    "type": "native_capability_unknown",
    "message": "provider/model/endpoint must pass native tool probe before handling tools requests"
  }
}
```

---

## 11. 验收标准

一个 provider 可以标记为 `native_tools=true`，必须满足：

1. 能通过 forced tool probe。
2. 返回里有协议级 tool call 字段，不是文本 JSON。
3. 参数可以被正常解析。
4. 工具结果可以用该协议标准格式回传，并得到最终回答。
5. streaming 模式如果声明支持，必须能收到标准 tool-call delta/event。
6. 失败时不会自动降级为 prompt fake。

验收命令建议：

```bash
# Chat Completions native probe
curl http://127.0.0.1:8885/v1/native-tools/probe \
  -H 'Content-Type: application/json' \
  -d '{"path":"/v1/chat/completions","model":"目标模型"}'

# Responses native probe
curl http://127.0.0.1:8885/v1/native-tools/probe \
  -H 'Content-Type: application/json' \
  -d '{"path":"/v1/responses","model":"目标模型"}'

# Anthropic Messages native probe
curl http://127.0.0.1:8885/v1/native-tools/probe \
  -H 'Content-Type: application/json' \
  -d '{"path":"/v1/messages","model":"目标模型"}'
```

---

## 12. 分阶段落地

### 阶段 1：Native passthrough

目标：先让 Claude Code / Codex 能拿到真实 `tool_calls/tool_use`。

实现：

- OpenAI Chat passthrough
- Anthropic Messages passthrough
- Responses passthrough
- native probe
- forced tool verification
- 禁止 prompt fallback

### 阶段 2：Provider registry

目标：多个 API 自动选择真实支持 tools 的后端。

实现：

- provider 配置文件
- capability cache
- 启动时/手动 probe
- `native_tools=true/false/unknown`

### 阶段 3：Server-side orchestration

目标：对接外部已实现工具，让普通业务 API 一次请求拿最终答案。

实现：

- tool executor registry
- tool schema validation
- tool result protocol 回填
- 多轮 loop
- timeout/audit

### 阶段 4：Streaming native tools

目标：支持 Claude Code / Codex 更完整的实时体验。

实现：

- OpenAI SSE tool call delta
- Responses event stream
- Anthropic event stream
- 不完整 JSON arguments 拼接
- tool event trace

### 阶段 5：安全和生产化

实现：

- API key 管理
- per-tool 权限
- rate limit
- request/response redaction
- audit log
- replay/debug trace
- health check + readiness probe

---

## 13. 最终推荐

最正确的方向：

```text
不要做 prompt fake shim。
做 Native Tool Gateway。
```

具体策略：

1. 对 Claude Code / Codex：默认用 Native Passthrough。
2. 对业务后端：可选 Server-side Native Tool Orchestration。
3. 对每个 provider/model/endpoint：必须 probe 后才能标记 native tools 可用。
4. 对不支持 tools 的普通 chat API：启用文本级工具调用解析作为 fallback。
5. 所有失败都显式返回，不假装支持。
