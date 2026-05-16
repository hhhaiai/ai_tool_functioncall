# Full Gateway Tool Runtime：完整 tool/function-call 支持与工具市场方案

## 1. 当前目标

本方案修正为：Gateway 不只是 native passthrough，也不只是补充少量 gateway-owned tools，而是要成为一个 **完整的 tool/function-call runtime**。

用户使用 Codex / OpenCode / Claude Code / DeepSeek-TUI 等客户端时，请求会先到本项目 Gateway。Gateway 负责：

1. 对外兼容主流对话 API：
   - `/v1/chat/completions`
   - `/v1/responses`
   - `/v1/messages`
2. 对内调用上游 AI。
3. 接收上游 AI 返回的真实协议级 tool/function call。
4. 在 Gateway 内部寻找、安装、路由、执行对应工具。
5. 把 tool/function result 合并回上游 AI。
6. 如果 AI 继续要求 function call，则继续执行循环。
7. 最终把 AI 处理后的结果返回给客户端。

核心判断：

> Gateway 要完整支持 tool call / function call，关键不是把所有工具手写死，而是实现一个可扩展的 Tool Runtime + Tool Marketplace/Connector 机制。任何功能要能执行，都必须能被解析为一个真实 executor：内置函数、MCP server、OpenAPI connector、HTTP action、外部 function service、脚本插件或 provider-native tool。

---

## 2. “完整支持”的准确含义

完整支持不是指 Gateway 天然知道世界上所有工具怎么执行，而是指 Gateway 具备以下能力：

```text
任意请求里的 tool schema
  -> Gateway 识别 function/tool 名称与参数 schema
  -> Gateway 查询本地 registry / marketplace / connector catalog
  -> 找到或安装对应 executor
  -> 执行 executor
  -> 转换结果为标准 tool_result/function_call_output
  -> 回填给上游 AI
```

所以完整支持分两层：

| 层 | Gateway 必须支持什么 | 说明 |
|---|---|---|
| 协议层 | 任意标准 tool/function call 格式 | Chat `tool_calls`、Responses `function_call`、Anthropic `tool_use` |
| 执行层 | 任意可被 registry/marketplace 映射到 executor 的工具 | MCP、OpenAPI、HTTP action、内置函数、脚本插件、外部服务 |

如果一个工具名没有任何 executor，也无法从 marketplace 安装，Gateway 不能伪造成功，只能：

1. 返回明确错误；或
2. 根据策略把 tool call 暂停/上报；或
3. 要求安装/配置 connector。

这仍然是真实支持，因为它没有 fake tool result。

---

## 3. 总体架构

```text
Codex / OpenCode / Claude Code / DeepSeek-TUI / SDK / App
        │
        │ OpenAI/Anthropic-compatible request
        ▼
┌──────────────────────────────────────────────────────────────┐
│ Full Gateway Tool Runtime                                     │
│                                                              │
│ 1. API Protocol Layer                                         │
│    - /v1/chat/completions                                     │
│    - /v1/responses                                            │
│    - /v1/messages                                             │
│                                                              │
│ 2. Upstream Model Adapter                                     │
│    - OpenAI-compatible                                        │
│    - Responses-compatible                                     │
│    - Anthropic-compatible                                     │
│                                                              │
│ 3. Tool Schema Normalizer                                     │
│    - OpenAI tools                                             │
│    - Responses tools                                          │
│    - Anthropic tools                                          │
│    - MCP tools                                                │
│    - OpenAPI actions                                          │
│                                                              │
│ 4. Tool Runtime Registry                                      │
│    - installed tools                                          │
│    - tool ownership                                           │
│    - executor mapping                                         │
│    - permission policy                                        │
│                                                              │
│ 5. Tool Marketplace / Connector Catalog                       │
│    - MCP servers                                              │
│    - OpenAPI specs                                            │
│    - HTTP actions                                             │
│    - plugin packages                                          │
│    - script tools                                             │
│    - external function-call services                          │
│                                                              │
│ 6. Tool Execution Engine                                      │
│    - execute function                                         │
│    - call MCP tools/call                                      │
│    - call HTTP API                                            │
│    - run sandboxed script                                     │
│    - call provider-native tool                                │
│                                                              │
│ 7. Orchestration Loop                                         │
│    - detect tool call                                         │
│    - execute tool                                             │
│    - append tool result                                       │
│    - call upstream again                                      │
│    - repeat until final                                       │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
Upstream AI Provider
```

---

## 4. Tool Runtime 的核心模型

Gateway 内部不应该把某一家 API 的 tool 格式作为核心结构，而应该归一化成统一模型。

### 4.1 ToolDefinition

```json
{
  "name": "github_create_issue",
  "description": "Create a GitHub issue",
  "input_schema": {
    "type": "object",
    "properties": {
      "repo": {"type": "string"},
      "title": {"type": "string"},
      "body": {"type": "string"}
    },
    "required": ["repo", "title"]
  },
  "source": "mcp|openapi|http_action|builtin|plugin|script|provider",
  "executor_id": "github-mcp:create_issue",
  "permission": {
    "risk": "write_external_service",
    "approval": "required"
  }
}
```

### 4.2 ToolCall

```json
{
  "protocol": "chat_completions|responses|anthropic_messages",
  "call_id": "call_123",
  "name": "github_create_issue",
  "arguments": {
    "repo": "owner/project",
    "title": "Bug report",
    "body": "..."
  },
  "raw": {}
}
```

### 4.3 ToolResult

```json
{
  "call_id": "call_123",
  "name": "github_create_issue",
  "success": true,
  "content": "{\"issue_url\":\"https://github.com/owner/project/issues/1\"}",
  "metadata": {
    "duration_ms": 831,
    "executor_id": "github-mcp:create_issue"
  }
}
```

---

## 5. Tool Marketplace / Function Marketplace

更准确的名称建议叫：

```text
Tool Marketplace
Connector Marketplace
Function Connector Registry
Action Marketplace
```

它不是模型能力市场，而是 **真实执行器市场**。

### 5.1 市场里每个工具包包含什么

一个工具包至少需要：

```json
{
  "id": "github",
  "name": "GitHub Tools",
  "version": "1.0.0",
  "provider": "mcp",
  "entry": {
    "type": "mcp_server",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"]
  },
  "auth": {
    "type": "env",
    "required": ["GITHUB_TOKEN"]
  },
  "tools": "dynamic",
  "permissions": ["network", "external_write"]
}
```

或者 OpenAPI 连接器：

```json
{
  "id": "orders-api",
  "provider": "openapi",
  "spec_url": "https://internal.example.local/openapi.json",
  "auth": {
    "type": "bearer",
    "env": "ORDERS_API_TOKEN"
  },
  "base_url": "https://internal.example.local"
}
```

### 5.2 市场接入后的流程

```text
用户启用工具包
  -> Gateway 安装/加载 connector
  -> 拉取 tools schema
  -> 转成内部 ToolDefinition
  -> 合并到对上游 AI 暴露的 tools
  -> 上游 AI 返回 tool call
  -> Gateway 找到 executor
  -> 执行并回填结果
```

### 5.3 市场来源优先级

建议支持这些来源：

| 来源 | 作用 | 优先级 |
|---|---|---|
| 本地内置工具 | probe、calculator、time、少量安全工具 | P0 |
| 本地插件目录 | 私有业务工具、本地 Python/JS 脚本 | P0 |
| MCP server catalog | 大量现成工具能力 | P0/P1 |
| OpenAPI connector | 内部系统和 SaaS API | P1 |
| HTTP action catalog | 简单函数服务 | P1 |
| 第三方 action 平台 | SaaS 自动化工具 | P2 |
| provider-native tools | 上游模型内置工具 | P2 |

---

## 6. Gateway 执行任意 tool 的策略

Gateway 收到上游 AI 的 tool call 后，按下面顺序处理：

```text
1. registry 里已有 executor
   -> 直接执行

2. registry 未命中，但 marketplace 有同名/别名工具
   -> 如果已授权自动安装，则安装并执行
   -> 如果需要授权，则返回 pending/install_required 错误或走审批流程

3. registry 未命中，但 tool schema 可映射到 OpenAPI operation
   -> 生成临时 connector，校验权限后执行

4. registry 未命中，但是 provider-native tool
   -> 交给 provider，不在 Gateway 执行

5. 无法找到 executor
   -> 返回 ToolNotFound / ConnectorRequired，不能伪造结果
```

伪代码：

```python
def resolve_executor(tool_call):
    if registry.has(tool_call.name):
        return registry.get(tool_call.name)

    candidate = marketplace.find(tool_call.name, tool_call.arguments_schema)
    if candidate and policy.allow_auto_install(candidate):
        executor = marketplace.install(candidate)
        registry.register(executor)
        return executor

    if openapi_mapper.can_map(tool_call):
        return openapi_mapper.build_executor(tool_call)

    if provider_tool_registry.is_provider_native(tool_call):
        return ProviderNativeExecutor(tool_call)

    raise ToolNotFound(tool_call.name)
```

---

## 7. Orchestration Loop

Gateway 是完整 tool-call loop 的 owner。

```python
def run_model_with_tools(path, request):
    state = prepare_request(path, request)

    for round_no in range(MAX_TOOL_ROUNDS):
        response = upstream.call(path, state.request_body)
        tool_calls = protocol.extract_tool_calls(path, response)

        if not tool_calls:
            return response

        results = []
        for call in tool_calls:
            executor = resolve_executor(call)
            result = execute_with_policy(executor, call)
            results.append(result)

        state.request_body = protocol.append_tool_results(
            path=path,
            request_body=state.request_body,
            assistant_response=response,
            tool_results=results,
        )

    raise MaxToolRoundsExceeded(MAX_TOOL_ROUNDS)
```

关键要求：

- 优先解析协议级 tool call。
- 当协议级 tool call 不存在时，解析文本中的 Claude-Code-like 标记（如 `<function=Glob>`），执行真实工具并回填结果。
- 每轮可执行多个 tool call。
- 支持并发执行 read-only 工具。
- 写入/外部副作用工具需要权限策略。
- 超过最大轮数直接失败。

---

## 8. 协议回填

### 8.1 Chat Completions

上游返回：

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

Gateway 追加：

```json
{
  "role": "tool",
  "tool_call_id": "call_1",
  "content": "3"
}
```

### 8.2 Responses

上游返回：

```json
{
  "output": [
    {
      "type": "function_call",
      "call_id": "call_1",
      "name": "calculator",
      "arguments": "{\"expression\":\"1+2\"}"
    }
  ]
}
```

Gateway 追加：

```json
{
  "type": "function_call_output",
  "call_id": "call_1",
  "output": "3"
}
```

### 8.3 Anthropic Messages

上游返回：

```json
{
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_1",
      "name": "calculator",
      "input": {"expression": "1+2"}
    }
  ],
  "stop_reason": "tool_use"
}
```

Gateway 追加：

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_1",
      "content": "3"
    }
  ]
}
```

---

## 9. 要不要兼容 Codex / Claude Code 的本地工具名

如果 Gateway 要“完整接管 tool loop”，就要支持这些客户端常用工具名对应的 executor。否则上游 AI 可能返回 `Read`、`Bash`、`apply_patch`，Gateway 找不到执行器。

因此有两种实现等级：

### 9.1 标准业务 Gateway

只执行 Gateway 注册工具，例如：

```text
calculator
query_internal_docs
get_order_status
github_create_issue
```

适合普通业务应用。

### 9.2 Agent-compatible Gateway

额外实现 Codex / Claude Code / OpenCode / DeepSeek-TUI 常用工具兼容包：

```text
Read / Write / Edit / Bash / Glob / Grep / apply_patch
exec_command / write_stdin / shell_command
web_search / fetch_url
request_user_input
```

这时 Gateway 需要自己提供：

- workspace mount / cwd
- 文件系统沙箱
- shell 沙箱
- patch 应用器
- 读写权限策略
- 审批策略
- 长任务/交互 session
- stdout/stderr 捕获
- 超时与取消

否则不能声称真正完整支持 coding-agent 类工具。

建议路线：

```text
第一阶段：标准业务 Gateway + marketplace connectors
第二阶段：只读 agent-compatible tools：Read/Glob/Grep/fetch_url/web_search
第三阶段：写入和 shell：Write/Edit/apply_patch/Bash/exec_command
第四阶段：交互和子代理：write_stdin/request_user_input/spawn_agent
```

---

## 10. 安全与权限

完整执行任意工具必须有权限层，否则 Gateway 会变成远程代码执行入口。

### 10.1 工具风险等级

| 风险 | 示例 | 默认策略 |
|---|---|---|
| pure | calculator、time | 自动执行 |
| read_local | Read、Glob、Grep | 限定 workspace |
| read_network | fetch_url、web_search | 域名 allowlist / rate limit |
| write_local | Write、Edit、apply_patch | 需要 workspace + 审批 |
| execute_code | Bash、exec_command | 默认关闭或强沙箱 |
| external_write | GitHub issue、Slack post、订单修改 | 需要审批/审计 |
| secret_access | 读密钥、数据库管理 | 默认禁止 |

### 10.2 必须有的控制项

```text
GATEWAY_MAX_TOOL_ROUNDS
GATEWAY_TOOL_TIMEOUT_SECONDS
GATEWAY_TOOL_ALLOWLIST
GATEWAY_TOOL_DENYLIST
GATEWAY_MARKETPLACE_AUTO_INSTALL
GATEWAY_REQUIRE_APPROVAL_FOR_WRITE
GATEWAY_WORKSPACE_ROOT
GATEWAY_NETWORK_ALLOWLIST
GATEWAY_AUDIT_LOG
```

---

## 11. MVP 实现范围

### 11.1 P0：完整 loop 骨架

- ToolCall extractor：Chat / Responses / Messages
- ToolResult appender：Chat / Responses / Messages
- Max rounds
- Tool execution registry
- Tool not found error result
- Non-streaming only
- 文本级 tool call 解析（Claude-Code-like 标记）

### 11.2 P0：内置工具

```text
calculator
echo_probe
get_current_time
```

### 11.3 P1：Connector/Marketplace

```text
MCP connector：stdio initialize + tools/list + tools/call + session pool + catalog cache + health snapshot + failure invalidation/reconnect 已实现基础版，工具名映射为 mcp__server__tool
OpenAPI connector
HTTP action connector：已实现基础版，可把配置里的 HTTP endpoint 暴露为真实 tool/function executor
local plugin connector
```

### 11.4 P2：Agent-compatible tool pack

```text
Read
Glob
Grep
fetch_url
web_search
apply_patch
Bash / exec_command
```

P2 必须配套 sandbox/permission，不能裸执行。

---

## 12. 测试清单

### 12.1 协议 loop

- Chat tool call -> execute -> tool message -> final answer
- Responses function_call -> execute -> function_call_output -> final answer
- Messages tool_use -> execute -> tool_result -> final answer
- 多轮 function call
- 并行多个 tool call
- max rounds 超限

### 12.2 Marketplace

- registry 已安装工具直接执行
- marketplace 命中工具并安装
- marketplace 缺少认证时报错
- unknown tool 返回 ToolNotFound，不 fake
- OpenAPI operation 映射成功
- MCP tools/list + tools/call 成功

### 12.3 安全

- 写文件越界失败
- shell 默认拒绝
- timeout 生效
- denylist 优先于 allowlist
- external write 需要审批
- audit log 记录 tool name / arguments hash / result status

---

## 13. 失败记录与持续迭代机制

Gateway 要越用越强，关键是不能把失败只当成一次请求错误丢掉。所有无法执行、执行失败、权限不足、schema 不匹配、marketplace 未命中的 tool/function call 都要结构化记录，进入后续维护和迭代流程。

### 13.1 需要记录哪些失败

| 类型 | 示例 | 后续价值 |
|---|---|---|
| `tool_not_found` | 模型调用了 `github_create_issue`，registry 没有 executor | 判断是否要从 marketplace 安装或新增 connector |
| `connector_not_installed` | marketplace 有 GitHub MCP，但未安装 | 提示用户启用/授权 |
| `auth_missing` | 缺少 `GITHUB_TOKEN` / API key | 生成配置引导 |
| `schema_mismatch` | 模型参数和 executor schema 不一致 | 改 schema、加 alias、优化 tool description |
| `permission_denied` | 写文件、shell、外部写入被策略拒绝 | 调整权限/审批策略 |
| `execution_failed` | HTTP 500、MCP call failed、脚本异常 | 修 executor 或增加重试/降级 |
| `timeout` | 工具执行超时 | 调整 timeout、异步任务化、结果轮询 |
| `unsafe_request` | 越权路径、危险 shell、外部副作用 | 加强 denylist/sandbox |
| `provider_protocol_error` | 上游返回 malformed tool call | provider capability 降级或 adapter 修复 |

### 13.2 失败事件结构

建议每次失败写入 JSONL，方便后续统计和自动修复：

```json
{
  "ts": "2026-05-14T12:00:00Z",
  "request_id": "req_xxx",
  "conversation_id": "conv_xxx",
  "round": 2,
  "protocol": "chat_completions",
  "model": "upstream-model",
  "tool_name": "github_create_issue",
  "failure_type": "tool_not_found",
  "arguments_hash": "sha256:...",
  "arguments_sample": {"repo": "owner/repo"},
  "tool_schema_hash": "sha256:...",
  "resolver_trace": [
    "registry:miss",
    "marketplace:github matched but not installed",
    "auth:GITHUB_TOKEN missing"
  ],
  "user_visible_error": "ConnectorRequired: GitHub tools are not installed",
  "recommended_action": "install_connector",
  "candidate_connector": "mcp/github",
  "fake_prompt_tools": false
}
```

注意：

- 参数要脱敏，默认只存 hash 和安全 sample。
- secret、完整文件内容、完整 shell 输出默认不能进失败库。
- 每条失败都要标记 `fake_prompt_tools=false`，证明没有伪造结果。

### 13.3 失败后的运行时行为

失败不能都直接 500。建议按类型处理：

```text
tool_not_found / connector_not_installed
  -> 生成协议级 tool error result，让模型知道工具不可用
  -> 同时写入 backlog，供后续安装/实现

auth_missing
  -> 返回清晰配置错误
  -> 记录需要哪些 env/config

schema_mismatch
  -> 返回 tool error result
  -> 记录原始 schema 与参数 shape

execution_failed / timeout
  -> 返回 tool error result
  -> 记录 executor、耗时、错误摘要

unsafe_request / permission_denied
  -> 返回拒绝结果
  -> 记录策略命中原因
```

### 13.4 维护队列

失败事件需要汇总成维护队列，而不是堆日志。

```text
failures.jsonl
  -> 每日/每 N 次请求聚合
  -> 生成 tool_backlog.json
  -> 分组：缺工具 / 缺认证 / schema 问题 / executor bug / 安全拒绝
  -> 给出优先级
  -> 进入迭代
```

`tool_backlog.json` 示例：

```json
{
  "missing_tools": [
    {
      "tool_name": "github_create_issue",
      "count": 37,
      "first_seen": "2026-05-14T10:00:00Z",
      "last_seen": "2026-05-14T12:00:00Z",
      "candidate_connectors": ["mcp/github", "openapi/github"],
      "priority": "high",
      "decision": "install_connector"
    }
  ],
  "schema_issues": [
    {
      "tool_name": "query_internal_docs",
      "count": 12,
      "issue": "model sends `q`, executor expects `query`",
      "decision": "add_alias"
    }
  ]
}
```

### 13.5 自动增强流程

Gateway 可以逐步具备自增强能力：

```text
1. 记录失败
2. 聚合同类失败
3. 查询 marketplace 是否有 connector
4. 如果安全且用户允许 auto-install：安装 connector
5. 如果缺配置：生成配置提示
6. 如果 schema mismatch：生成 alias/adapter patch 候选
7. 如果 executor bug：进入测试用例和修复队列
8. 修复后 replay 历史失败样本验证
9. 成功后标记 resolved
```

### 13.6 必须补的测试

每个失败类别都要有回归测试：

```text
unknown tool -> tool error result + failure log
marketplace match but not installed -> connector_required + backlog
auth missing -> config_required + no secret leakage
schema mismatch -> error result + alias recommendation
timeout -> error result + timeout metric
permission denied -> refusal result + policy trace
resolved backlog -> replay succeeds
```

### 13.7 产品侧展示

设置页建议增加：

```text
Tools Runtime
  - 已安装工具数
  - 可用 marketplace connectors
  - 失败工具调用
  - 建议安装的 connectors
  - 缺失的认证配置
  - 最近 resolved 的工具能力
```

这样用户能看到系统不是“失败了就没了”，而是在持续积累能力。

---

## 14. 工具接入策略、稳定性与服务质量

为了适配编程及相关 agent 场景，Gateway 需要同时具备两类能力：

```text
1. Runtime + 核心工具：我们自己实现，作为稳定底座。
2. 外部工具生态：通过 MCP / OpenAPI / action / plugin marketplace 接入。
```

关键问题是：工具到底预先安装，还是遇到时再检索安装？推荐采用 **分层策略**，不能只选一种。

### 14.1 分层接入策略

| 层级 | 策略 | 示例 | 目标 |
|---|---|---|---|
| P0 核心 runtime | 内置实现，随 Gateway 发布 | tool loop、schema normalizer、tool result appender、failure recorder、permission engine | 保证基础能力稳定 |
| P0 编程核心工具 | 预装/内置 | `Read`、`Glob`、`Grep`、`fetch_url`、`calculator`、`echo_probe`、`get_current_time` | 适配 coding agent 的高频只读能力 |
| P1 写入/执行工具 | 默认安装但受权限控制 | `apply_patch`、`Edit`、`Write`、`Bash`、`exec_command` | 支持完整编程闭环，但必须 sandbox/approval |
| P1 常用 connector | 预装 catalog，不默认启用密钥 | GitHub、Postgres、Browser、Docs、Search、Filesystem MCP | 降低首次使用失败率 |
| P2 业务/私有工具 | 管理员预配置 | 内部文档、订单、CRM、数据库、公司 API | 保证业务可用性 |
| P3 长尾工具 | 遇到时检索、建议安装 | Notion、Slack、Jira、特殊 SaaS | 覆盖长尾需求 |
| P4 未知工具 | 记录失败，进入 backlog | marketplace 无匹配 | 后续补 connector 或手写实现 |

因此推荐默认行为：

```text
基础编程能力：预装/内置
常用市场连接器：预索引，可一键启用
业务关键工具：部署前预配置并做健康检查
长尾工具：遇到时检索、安装、授权
无法解析工具：记录失败并进入迭代队列
```

### 14.2 为什么不能只做按需安装

只做按需安装会影响稳定性：

```text
用户请求中途触发未知工具
  -> Gateway 才去检索 marketplace
  -> 安装可能失败
  -> 认证可能缺失
  -> schema 可能不兼容
  -> 当前对话中断
```

所以高频和关键工具必须提前就绪。按需安装适合长尾工具，不适合作为核心能力保障。

### 14.3 如何“保证必然能实现”

严格说，不能承诺“世界上任意 function call 都必然可执行”。真正可保证的是：

```text
1. 对已注册、已安装、已授权的工具：保证可执行。
2. 对 marketplace 可匹配的工具：保证可安装或给出明确安装/授权路径。
3. 对无法匹配的工具：保证不伪造结果，记录失败并进入补齐流程。
4. 对核心编程工具：通过内置 runtime + sandbox + 测试矩阵保证稳定支持。
```

也就是服务承诺应该写成：

```text
Gateway 保证完整执行所有“已声明可用”的 tool/function call。
未声明、未安装、未授权的工具不会假装成功，会被记录、聚合、推荐安装或进入实现 backlog。
```

这比“任何名字都能执行”更真实，也更稳定。

### 14.4 Capability Contract

每个 provider / workspace / user session 都应该有能力清单：

```json
{
  "runtime": {
    "tool_loop": true,
    "max_rounds": 5,
    "streaming_tools": false
  },
  "tools": [
    {"name": "Read", "status": "ready", "source": "builtin", "risk": "read_local"},
    {"name": "Grep", "status": "ready", "source": "builtin", "risk": "read_local"},
    {"name": "apply_patch", "status": "ready_with_approval", "source": "builtin", "risk": "write_local"},
    {"name": "github_create_issue", "status": "auth_required", "source": "mcp/github", "risk": "external_write"}
  ]
}
```

Gateway 只应把 `ready` 或策略允许的 `ready_with_approval` 工具暴露给上游 AI。

不要把不可执行工具暴露给模型，否则模型会调用，最终造成失败。

### 14.5 工具暴露策略

```text
ready
  -> 可以暴露给模型

ready_with_approval
  -> 可以暴露，但 description 要说明需要审批

auth_required
  -> 默认不暴露；设置页提示用户配置

install_required
  -> 默认不暴露；可在失败后推荐安装

broken / failing
  -> 不暴露；进入维护队列

unknown
  -> 不暴露；只记录和检索
```

这样可以显著减少模型调用不可用工具。

### 14.6 稳定性保障

稳定性需要从 runtime、工具、连接器、上游 API 四层保障。

#### Runtime 层

```text
- max tool rounds
- per-tool timeout
- circuit breaker
- retry with backoff
- concurrency limit
- idempotency key
- cancellation
- structured error result
- audit log
```

#### 工具层

```text
- JSON schema 校验
- 参数规范化/alias
- 输出大小限制
- 大结果摘要/分页/handle
- side-effect 工具审批
- sandbox
- replay tests
```

#### Connector 层

```text
- connector health check
- auth preflight
- version pinning
- schema hash
- tool catalog cache
- broken connector 自动下线
```

#### 上游模型层

```text
- native tool probe
- forced tool choice probe
- tool-result roundtrip probe
- malformed tool call detection
- provider capability cache
```

### 14.7 服务质量 SLO

建议定义可量化指标：

| 指标 | 目标 | 说明 |
|---|---:|---|
| 已声明 ready 工具执行成功率 | >= 99% | 不含外部服务自身故障 |
| tool resolver 命中率 | >= 95% | 高频场景应命中 registry/catalog |
| tool_not_found 率 | 持续下降 | 用于衡量 marketplace 覆盖 |
| schema_mismatch 率 | < 1% | 靠 schema/alias/description 迭代 |
| P95 tool 执行耗时 | 按工具分类 | pure/read/write/network 分别统计 |
| max_rounds_exceeded 率 | < 0.5% | 过高说明模型/tool loop 设计有问题 |
| fake tool result | 0 | 永远不能伪造成功 |

### 14.8 质量门禁

一个工具进入 `ready` 状态前必须通过：

```text
1. schema 校验测试
2. 正常执行测试
3. 错误输入测试
4. timeout 测试
5. 权限/越权测试
6. tool result 回填测试
7. replay 历史失败样本
8. 文档/描述检查
```

Marketplace connector 进入 `ready` 前还要通过：

```text
1. 安装测试
2. auth preflight
3. tools/list 测试
4. tools/call smoke test
5. schema hash 固定
6. 版本兼容检查
```

### 14.9 推荐落地方式

```text
第一阶段：内置完整 runtime + P0/P1 编程核心工具
第二阶段：预置 MCP/OpenAPI/action catalog，但只暴露 ready 工具
第三阶段：失败记录 -> backlog -> 自动推荐 connector
第四阶段：允许安全的按需安装
第五阶段：基于真实失败 replay 持续提升成功率
```

结论：

> 稳定性来自“预装核心能力 + ready 才暴露 + 按需补长尾 + 失败持续迭代”，而不是运行时盲目承诺所有未知工具都能立即执行。

---

## 15. 最终原则

1. Gateway 是完整 tool/function-call runtime。
2. function call 只是协议，真实能力来自 executor。
3. executor 可以来自内置实现、MCP、OpenAPI、HTTP action、插件、脚本或外部 function service。
4. 可以做 tools/function marketplace，但市场卖的是连接器/执行器，不是 prompt 模板。
5. 未安装/未授权/找不到 executor 时必须明确失败，不能伪造工具结果。
6. 如果要兼容 coding agent 的本地工具名，Gateway 必须实现对应 sandbox/runtime。
7. 非流式先稳定，streaming tool events 后续实现。
