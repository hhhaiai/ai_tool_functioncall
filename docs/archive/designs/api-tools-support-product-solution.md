# API Tools 支持功能：产品级解决方案

## 1. 功能定义

这个能力定义为一个独立功能：**API Tools 支持**。

目标不是让普通 chat API 通过 prompt 假装支持 tools，而是让系统具备一套可验证、可配置、可回退的 tools/function-call 能力：

1. 用户可以在设置里选择某个 API 是否启用 tools。
2. 系统可以自动测试该 API 是否真的支持 native tools。
3. 如果 API 原生支持，就走 native passthrough。
4. 如果 API 不支持，但用户仍需要 tools，就走我们的 **协议适配层**。
5. 协议适配层必须对接真实工具执行能力或真实 function-call 服务，不能靠 prompt JSON 冒充。
6. Claude Code / Codex / OpenCode 等 coding agent 的主流工具需要有统一抽象和适配策略。

---

## 2. 设置入口设计

### 2.1 Provider 级设置

每个 API provider 增加一个 tools 设置区：

```json
{
  "provider_id": "my-api",
  "base_url": "https://api.example.com/v1",
  "api_key": "env:MY_API_KEY",
  "model": "model-name",
  "tools": {
    "enabled": "auto",
    "mode": "auto",
    "native_probe": true,
    "fallback_adapter": "internal",
    "fail_if_unverified": true,
    "allowed_toolsets": ["core_coding", "web", "mcp"],
    "denied_tools": ["shell.unrestricted"],
    "approval_policy": "ask_for_write_and_shell"
  }
}
```

### 2.2 `tools.enabled`

| 值 | 含义 |
|---|---|
| `off` | 不传 tools，不启用 function call。只做普通对话。 |
| `auto` | 系统自动探测。支持 native 就启用；不支持则按 fallback 策略。推荐默认。 |
| `on` | 用户明确要求启用 tools。若 native 不支持，必须走 adapter 或失败。 |
| `native_only` | 必须上游原生支持；不支持直接失败。用于 Claude Code/Codex 这种需要真实协议的场景。 |

### 2.3 `tools.mode`

| 值 | 含义 |
|---|---|
| `native_passthrough` | 原样透传 native tool calls，客户端执行工具。适合 Claude Code / Codex。 |
| `server_orchestrated` | 网关收到 native tool call 后，服务端执行工具并回填 tool result。适合业务 API。 |
| `adapter` | 上游不支持 native tools 时，转到我们的协议适配层。注意：adapter 仍必须是真实工具协议，不是 prompt 模拟。 |
| `auto` | 根据 provider probe 和客户端类型自动选择。 |

### 2.4 UI 文案建议

设置入口可以叫：

```text
API Tools 支持
```

可选项：

```text
关闭
自动检测（推荐）
强制原生支持
使用内部工具适配层
```

状态展示：

```text
Native Tools: 已验证 / 不支持 / 未测试 / 测试失败
Function Call: 已验证 / 不支持 / 未测试 / 测试失败
Streaming Tool Events: 已验证 / 不支持 / 未测试
Fallback Adapter: 可用 / 不可用
```

按钮：

```text
测试 tools 支持
查看探测详情
查看工具映射
```

---

## 3. 自动测试机制

### 3.1 为什么必须测试

很多 API 声称兼容 OpenAI，但实际情况可能是：

- `tools` 字段直接 400。
- 接受 `tools` 但完全忽略。
- 能返回普通文本 JSON，但没有协议级 `tool_calls`。
- 只部分模型支持。
- `tool_choice=auto` 可用，但 forced tool_choice 不可用。
- stream 模式工具事件不完整。

因此不能只看文档或配置，必须每个 provider/model/endpoint 做 probe。

### 3.2 Probe 维度

每个 API 至少测试：

```text
provider + model + endpoint + protocol + stream/non-stream
```

维度：

1. `/v1/chat/completions` native `tool_calls`
2. `/v1/responses` native `function_call`
3. `/v1/messages` native `tool_use`
4. forced tool choice
5. tool result 回填
6. streaming tool delta/event
7. 参数 JSON 是否可解析
8. 多工具并发/串行调用

### 3.3 Probe 通过标准

#### Chat Completions

必须返回：

```json
{
  "choices": [
    {
      "message": {
        "tool_calls": [
          {
            "type": "function",
            "function": {
              "name": "echo_probe",
              "arguments": "{...}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

#### Responses

必须返回：

```json
{
  "output": [
    {
      "type": "function_call",
      "name": "echo_probe",
      "call_id": "...",
      "arguments": "{...}"
    }
  ]
}
```

#### Anthropic Messages

必须返回：

```json
{
  "content": [
    {
      "type": "tool_use",
      "id": "...",
      "name": "echo_probe",
      "input": {...}
    }
  ],
  "stop_reason": "tool_use"
}
```

### 3.4 Capability Cache

测试结果保存为：

```json
{
  "provider_id": "my-api",
  "model": "model-name",
  "endpoint": "/v1/chat/completions",
  "native_tools": true,
  "forced_tool_choice": true,
  "tool_result_roundtrip": true,
  "streaming_tool_events": false,
  "tested_at": "2026-05-14T00:00:00Z",
  "evidence": {
    "request_id": "...",
    "response_shape": "choices[].message.tool_calls"
  }
}
```

缓存策略：

- `native_tools=true` 有效期建议 24 小时或跟随 provider 配置版本。
- `native_tools=false` 也缓存，但用户可手动重新测试。
- 模型名、base_url、endpoint、headers 变化后必须重新 probe。

---

## 4. 默认回退策略：我们的协议适配层

你说的“如果不支持，默认走我们的协议适配”是正确方向，但要明确：

> 协议适配不是 prompt fake。协议适配是把不同客户端/服务的工具协议统一起来，然后路由到真实工具执行器或真实 native-tools provider。

### 4.1 三层回退

```text
第一层：Native Passthrough
  上游 API 真实支持 tools，直接透传。

第二层：Native Provider Reroute
  当前 API 不支持 tools，但系统内有其他 provider 支持 tools，自动切到支持 tools 的 provider。

第三层：Internal Tool Protocol Adapter
  当前模型只做推理/文本，工具能力由我们内部 tool runtime 或外部 function-call 服务提供。
  注意：这里不能伪造模型原生 tool_calls 给 Claude Code/Codex，除非调用的外部 function-call 服务本身返回真实协议对象。
```

### 4.2 适配层职责

协议适配层包含：

1. **Tool Schema Registry**：统一保存工具 schema。
2. **Protocol Translator**：OpenAI Chat / Responses / Anthropic Messages / MCP / OpenCode config 互转。
3. **Tool Runtime**：真实执行工具，例如文件读写、shell、web fetch、MCP。
4. **Permission Engine**：读写、shell、网络、MCP 权限控制。
5. **Audit Log**：每次工具调用的输入、输出、耗时、权限决策。
6. **Capability Router**：决定走 native provider、reroute provider，还是 internal runtime。
7. **Verification Guard**：禁止把 prompt 结果伪装成 native tool call。

---

## 5. 主流 coding agent 工具调研

## 5.1 Claude Code 工具

Claude Code 官方工具参考里，工具覆盖读写、搜索、命令、子代理、计划、任务、MCP 资源、Web 等类别。典型工具包括：

| 类别 | 工具 |
|---|---|
| Shell | `Bash`, `PowerShell`, `Monitor` |
| 文件读取 | `Read` |
| 文件修改 | `Edit`, `Write`, `NotebookEdit` |
| 搜索 | `Glob`, `Grep`, `LSP` |
| Web | `WebFetch`, `WebSearch` |
| 任务/计划 | `TodoWrite`, `TaskCreate`, `TaskList`, `TaskUpdate`, `EnterPlanMode`, `ExitPlanMode` |
| 子代理/团队 | `Agent`, `SendMessage`, `TeamCreate` 等 |
| MCP | `ListMcpResourcesTool`, `ReadMcpResourceTool`, 以及 MCP server tools |
| 扩展 | `Skill`, hooks, plugins |
| 用户交互 | `AskUserQuestion` |

关键适配点：

- Claude Code 的工具有明确 permission rules。
- 写文件、shell、web 请求等需要权限控制。
- MCP 是扩展工具的主要方式。
- 工具名是权限和 hook matcher 的精确字符串。

## 5.2 Codex / OpenAI 工具

OpenAI API 层面主流 tools 包括：

| 类别 | 工具/能力 |
|---|---|
| Function calling | 自定义函数工具 |
| Web | web search |
| 文件 | file search / retrieval |
| 代码执行 | code interpreter |
| Shell | Responses API shell tool，可 hosted 或 local runtime |
| Patch | Responses API `apply_patch` tool |
| Computer use | computer use tool |
| MCP | MCP tools/connectors |

Codex CLI 产品层面通常体现为：

- 读文件
- 写文件/编辑代码
- 执行 shell 命令
- 应用 patch
- 按审批模式控制读写和命令执行
- 通过 MCP 扩展第三方工具

关键适配点：

- Shell 和 `apply_patch` 是 coding agent 的核心能力。
- OpenAI shell tool 是 Responses API 能力，不是 Chat Completions 能力。
- `apply_patch` 应该作为结构化 patch 工具，不应该退化成让模型输出一段自然语言 diff。
- Codex 类客户端更适合 native passthrough；工具执行由客户端沙箱/审批系统掌控。

## 5.3 OpenCode 工具

OpenCode 官方工具包括：

| 类别 | 工具 |
|---|---|
| Shell | `bash` |
| 文件修改 | `edit`, `write`, `apply_patch` |
| 文件读取 | `read` |
| 搜索 | `grep`, `glob`, `lsp` |
| 任务 | `todowrite` |
| Web | `webfetch`, `websearch` |
| 技能 | `skill` |
| 用户交互 | `question` |
| 扩展 | custom tools, MCP servers |

关键适配点：

- OpenCode 用 `permission` 控制工具行为：`allow` / `ask` / `deny`。
- `write` 和 `apply_patch` 归入 `edit` 权限。
- `grep` / `glob` 内部使用 ripgrep。
- MCP 支持 local 和 remote server。

---

## 6. 我们应该支持的工具集合

建议不要一开始追求完全复制每个客户端，而是定义自己的标准工具族。

### 6.1 Core Coding Toolset

第一优先级，必须支持：

| 标准工具 | 作用 | 对标 |
|---|---|---|
| `fs.read` | 读文件，支持 offset/limit | Claude `Read`, OpenCode `read` |
| `fs.write` | 新建/覆盖文件 | Claude `Write`, OpenCode `write` |
| `fs.edit` | 精确替换编辑 | Claude `Edit`, OpenCode `edit` |
| `fs.apply_patch` | 应用结构化 patch | OpenAI `apply_patch`, OpenCode `apply_patch`, Codex apply_patch |
| `search.grep` | 内容搜索 | Claude `Grep`, OpenCode `grep` |
| `search.glob` | 文件模式搜索 | Claude `Glob`, OpenCode `glob` |
| `shell.run` | 执行命令 | Claude `Bash`, OpenCode `bash`, OpenAI shell |
| `plan.update` | 计划/任务列表 | Claude `TodoWrite`/Task tools, OpenCode `todowrite`, Codex `update_plan` |
| `user.question` | 询问用户 | Claude `AskUserQuestion`, OpenCode `question` |

### 6.2 Web Toolset

第二优先级：

| 标准工具 | 作用 | 对标 |
|---|---|---|
| `web.fetch` | 拉取指定 URL | Claude `WebFetch`, OpenCode `webfetch` |
| `web.search` | Web 搜索 | Claude `WebSearch`, OpenCode `websearch`, OpenAI web search |

### 6.3 Code Intelligence Toolset

第三优先级：

| 标准工具 | 作用 | 对标 |
|---|---|---|
| `lsp.hover` | 类型/文档信息 | Claude `LSP`, OpenCode `lsp` |
| `lsp.definition` | 跳转定义 | Claude `LSP`, OpenCode `lsp` |
| `lsp.references` | 查找引用 | Claude `LSP`, OpenCode `lsp` |
| `lsp.symbols` | 文件/工作区符号 | Claude `LSP`, OpenCode `lsp` |
| `diagnostics.get` | 类型/语法诊断 | Claude `LSP` |

### 6.4 MCP Toolset

长期必须支持：

| 标准能力 | 作用 |
|---|---|
| `mcp.list_tools` | 列出 MCP server 暴露的 tools |
| `mcp.call_tool` | 调用 MCP tool |
| `mcp.list_resources` | 列出 MCP resources |
| `mcp.read_resource` | 读取 MCP resource |
| `mcp.oauth` | remote MCP OAuth/API key 管理 |

---

## 7. 协议映射策略

## 7.1 OpenAI Chat Completions 映射

标准工具转成：

```json
{
  "type": "function",
  "function": {
    "name": "fs_read",
    "description": "Read a file from the workspace",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {"type": "string"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"}
      },
      "required": ["path"]
    }
  }
}
```

返回必须是：

```json
{
  "tool_calls": [
    {
      "id": "call_xxx",
      "type": "function",
      "function": {
        "name": "fs_read",
        "arguments": "{\"path\":\"src/main.ts\"}"
      }
    }
  ]
}
```

工具结果回填：

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "..."
}
```

## 7.2 OpenAI Responses 映射

标准工具转成 Responses tools。

`apply_patch` 和 `shell` 优先使用 OpenAI 原生 tool 类型：

```json
{"type": "apply_patch"}
```

```json
{"type": "shell", "environment": {"type": "local"}}
```

如果 provider 不支持这些内置 tool 类型，再映射成 function tool，但必须由我们的 tool runtime 执行，不能让模型输出自然语言 patch。

## 7.3 Anthropic Messages 映射

标准工具转成：

```json
{
  "name": "fs_read",
  "description": "Read a file from the workspace",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"}
    },
    "required": ["path"]
  }
}
```

返回：

```json
{
  "type": "tool_use",
  "id": "toolu_xxx",
  "name": "fs_read",
  "input": {"path": "src/main.ts"}
}
```

工具结果：

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_xxx",
  "content": "..."
}
```

## 7.4 OpenCode 映射

OpenCode 本身支持工具配置和 MCP。我们的适配策略：

- 内部标准工具名用 snake/dot 风格，例如 `fs.read`。
- 暴露给 OpenCode 时映射为它习惯的工具名：`read`, `edit`, `bash`, `apply_patch`。
- 权限同步到 OpenCode 的 `permission` 字段。
- MCP server 直接以 OpenCode `mcp` 配置挂载。

---

## 8. 权限模型

必须把工具按风险分级。

| 风险级别 | 工具 | 默认策略 |
|---|---|---|
| 只读 | `fs.read`, `search.grep`, `search.glob`, `lsp.*` | allow |
| 网络读取 | `web.fetch`, `web.search` | ask 或按域名 allowlist |
| 写文件 | `fs.write`, `fs.edit`, `fs.apply_patch` | ask |
| 命令执行 | `shell.run` | ask，支持命令 allowlist/denylist |
| 外部系统 | MCP、数据库、GitHub、Sentry 等 | ask 或 per-server policy |
| 高危 | `rm -rf`, `sudo`, secrets 读取、生产操作 | deny by default |

权限决策输入：

```json
{
  "tool": "shell.run",
  "args": {"command": "npm test"},
  "workspace": "/repo",
  "provider": "my-api",
  "client": "codex",
  "risk": "write_or_exec"
}
```

权限结果：

```json
{
  "decision": "allow|ask|deny",
  "reason": "matched shell allowlist npm test",
  "matched_rule": "shell.run(npm test*)"
}
```

---

## 9. 内部模块设计

```text
api-tools-support/
  settings/
    provider-tools-settings.ts
    tools-ui-schema.ts
  probe/
    native-probe-runner.ts
    chat-completions-probe.ts
    responses-probe.ts
    anthropic-messages-probe.ts
  registry/
    provider-capability-registry.ts
    tool-schema-registry.ts
    client-tool-profile-registry.ts
  adapters/
    openai-chat-adapter.ts
    openai-responses-adapter.ts
    anthropic-messages-adapter.ts
    opencode-adapter.ts
    mcp-adapter.ts
  runtime/
    tool-runtime.ts
    fs-tools.ts
    shell-tool.ts
    search-tools.ts
    web-tools.ts
    lsp-tools.ts
    mcp-tool-runtime.ts
  permissions/
    permission-engine.ts
    risk-classifier.ts
  audit/
    tool-call-audit-log.ts
```

---

## 10. 运行时决策流程

```text
请求进入
  ↓
识别 client profile：claude-code / codex / opencode / generic-api
  ↓
读取 provider tools 设置
  ↓
请求是否包含 tools？
  ├─否：普通对话
  └─是：检查 capability cache
       ├─native_tools=true：native passthrough
       ├─native_tools=unknown：执行 probe
       │    ├─通过：native passthrough
       │    └─失败：进入 fallback 策略
       └─native_tools=false：进入 fallback 策略
  ↓
fallback 策略
  ├─native_only：失败
  ├─reroute：切到支持 native tools 的 provider
  ├─adapter：使用内部 tool runtime / 外部 function-call 服务
  └─off：普通对话但拒绝 tools 字段
```

---

## 11. 对 Claude Code / Codex / OpenCode 的建议默认策略

### Claude Code

```text
默认：native_passthrough
原因：Claude Code 自己有完整工具权限、hooks、MCP、沙箱和客户端执行链路。
不要在服务端替它执行本地文件/shell工具。
```

### Codex

```text
默认：native_passthrough
原因：Codex 的核心是本地读写、shell、apply_patch、审批和沙箱。
如果接 OpenAI Responses，可优先使用 shell/apply_patch 原生工具。
```

### OpenCode

```text
默认：adapter 或 native_passthrough，取决于接入方式
如果 OpenCode 作为客户端：native_passthrough。
如果我们要兼容 OpenCode 的工具生态：实现 OpenCode tool profile + MCP 映射。
```

### 普通业务 API

```text
默认：server_orchestrated
原因：业务方更希望一次请求拿最终答案，工具执行在服务端完成。
```

---

## 12. MVP 范围

第一版不要做太大，建议 MVP：

1. 设置入口：`off / auto / native_only / adapter`。
2. Probe：支持 Chat Completions、Responses、Messages 三类 forced tool probe。
3. Capability cache。
4. Native passthrough。
5. Internal adapter 支持最小 core coding toolset：
   - `fs.read`
   - `fs.apply_patch`
   - `search.grep`
   - `search.glob`
   - `shell.run`
   - `plan.update`
6. 权限模型：只读 allow，写和 shell ask，高危 deny。
7. 明确禁止 prompt fake fallback。

---

## 13. 验收标准

这个功能做完后，必须能回答：

1. 这个 provider 是否支持 tools？证据是什么？
2. 是 native 支持，还是 adapter 支持？
3. 如果是 native，返回里是否真的有 `tool_calls/tool_use/function_call`？
4. 如果是 adapter，工具是谁执行的？权限怎么控制？审计日志在哪里？
5. Claude Code / Codex 调用时，是否能拿到真实协议字段？
6. 不支持 tools 的 API 是否会明确失败，而不是假装成功？
7. 每个工具 schema、权限、输入输出是否可追踪？

---

## 14. 结论

你的思路可以整理为最终方向：

```text
API Tools 支持 = 设置入口 + 自动能力测试 + native 优先 + 真实协议适配 + 主流 coding-agent 工具映射 + 权限/审计。
```

最重要的原则：

```text
可以 adapter，但不能 fake。
可以 fallback，但 fallback 必须连接真实工具 runtime 或真实 native-tools provider。
```

推荐先落地：

```text
Settings UI → Probe → Capability Registry → Native Passthrough → Core Tool Adapter → Claude Code/Codex/OpenCode Profiles
```
