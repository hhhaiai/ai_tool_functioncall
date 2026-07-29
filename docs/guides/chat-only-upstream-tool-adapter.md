# Chat-only / Weak-tools 上游适配指南

> 最后校准：2026-07-29。本文说明如何让不可靠支持原生 tools/function calls 的上游，为 Claude Code、Codex 和 SDK 提供稳定的 Gateway 工具链。

## 适用场景

当上游出现任一情况时，使用 adapter：

- 请求含 `tools` / `tool_choice` 时返回 400；
- 接受工具字段但始终只返回文本；
- 不稳定返回 `tool_calls` / `function_call` / `tool_use`；
- Chat、Responses、Messages 三协议支持程度不一致；
- 没有 Claude Code 需要的 `/anthropic` 别名；
- 没有 direct `/v1/tools/call` / `/v1/functions/call`。

不要因为某一次 forced-tool probe 返回结构化调用，就把整个 Provider 标记为全面 native。Claude Code/Codex 需要路径、SSE、调用 ID、参数、结果回填和多轮行为同时稳定。

## 推荐配置

在被 Git 忽略的 `.gateway_service.json` 中配置真实 URL 和密钥：

```json
{
  "upstream": {
    "base_url": "<UPSTREAM_BASE_URL>",
    "api_key": "<UPSTREAM_API_KEY>",
    "model": "<MODEL>",
    "protocol": "openai_chat",
    "tools_enabled": "adapter",
    "native_tools_verified": false,
    "capabilities": {
      "supports_streaming": true,
      "supports_tools": false,
      "supports_function_calls": false,
      "supports_parallel_tool_calls": false,
      "supports_vision": false,
      "supports_network": false,
      "supports_web_search": false,
      "supports_json_schema": false
    }
  },
  "gateway": {
    "tool_mode": "orchestrate",
    "execute_user_side_tools_in_gateway": false,
    "local_planner_enabled": true,
    "text_tool_call_fallback_enabled": true,
    "text_tool_adapter_compact_token_limit": 48000,
    "max_tool_rounds": 10
  }
}
```

说明：

- `tools_enabled=adapter`：不把上游当作完整 native-tool Provider；
- `tool_mode=orchestrate`：由 Gateway 管理多轮工具生命周期；
- `execute_user_side_tools_in_gateway=false`：文件、shell、Skill、GUI 等返回给客户端；
- `local_planner_enabled=true`：使用 intent/workflow planner；
- `text_tool_call_fallback_enabled=true`：允许解析弱上游文本工具标记；
- `text_tool_adapter_compact_token_limit`：在构造弱上游工具提示前先压缩大 harness；
- `max_tool_rounds`：限制循环。

## 执行模型

```text
Claude Code / Codex request + tools
  → Gateway 规范化协议和 workspace scope
  → Planner 判断下一步
      ├─ Gateway-owned tool
      │    → Gateway 真执行
      │    → 将真实结果加入下一轮
      └─ downstream-owned tool
           → 返回原生 tool request
           → 客户端在用户机器执行
           → 客户端回传真实结果
  → 上游只接收有界证据和最终综合请求
  → Gateway 返回下游协议响应
```

### Gateway-owned 例子

- `calculator`、`get_current_time`；
- Memory/RecallMemory；
- MCP tools；
- Admin 配置的 HTTP Actions；
- 受控 `WebFetch` / `WebSearch`。

### Downstream-owned 例子

- Read/LS/Glob/Grep；
- Write/Edit/ApplyPatch；
- Bash/code interpreter；
- Skill/plugin；
- GUI/computer-use；
- local agent/subagent。

Gateway 不为 downstream-owned 工具生成假结果。

## 下游接入

### Claude Code

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8885/anthropic"
export ANTHROPIC_AUTH_TOKEN="$GATEWAY_DOWNSTREAM_KEY"
export ANTHROPIC_API_KEY=""
claude -p "Reply with OK only."
```

Claude Code 会访问 `/anthropic/v1/messages`；Gateway 规范化为 canonical `/v1/messages`。

### Codex / OpenAI SDK

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8885/v1"
export OPENAI_API_KEY="$GATEWAY_DOWNSTREAM_KEY"
```

Codex Responses 请求进入 `/v1/responses`，工具结果使用 `function_call_output` 关联 `call_id`。

## 最短验证

### 1. 启动和能力

```bash
./scripts/mimo_gateway.sh start
curl -fsS http://127.0.0.1:8885/healthz
curl -fsS http://127.0.0.1:8885/capabilities | python3 -m json.tool
```

### 2. Gateway-owned direct tool

```bash
curl -fsS http://127.0.0.1:8885/v1/tools/call \
  -H "Authorization: Bearer $GATEWAY_DOWNSTREAM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool":"calculator","arguments":{"expression":"123*456+7"}}'
```

此步骤不依赖上游模型，可先证明 Gateway runtime、认证和 direct tool path。

### 3. 协议级工具请求

使用 [`dialogue-curl-examples.md`](dialogue-curl-examples.md) 分别测试 Chat、Responses 和 Messages。观察：

- Gateway-owned 工具是否真实执行并继续综合；
- downstream-owned 工具是否返回正确协议字段；
- 客户端回传结果后是否继续下一轮；
- 最终综合是否不再携带未解决工具调用。

### 4. 自动化验收

```bash
./scripts/agent_planner_acceptance.sh
./scripts/agent_planner_acceptance.sh --full
./scripts/ci_gate.sh
```

## Native 能力升级条件

只有以下行为在同一 Provider/model 上稳定成立，才考虑把能力配置改为 native：

1. Chat、Responses 或 Messages 目标路径真实存在；
2. `tools` schema 被接受且不是静默忽略；
3. forced 和 auto tool choice 均返回协议级调用；
4. arguments 合法且调用 ID 稳定；
5. tool result 回填后能继续生成最终回答；
6. streaming 工具事件规范；
7. parallel calls、错误、timeout、重复和重试语义明确；
8. Claude Code/Codex 客户端 E2E 通过。

未满足时继续使用 adapter；可以只为已证明的单协议/单路径开启实验 profile，不要扩大为全局能力声明。

## 常见故障

| 现象 | 优先检查 |
|---|---|
| 上游只返回工具 JSON 文本 | `tools_enabled=adapter`、text fallback、planner event |
| 用户要求读文件却在 Gateway 服务目录执行 | `execute_user_side_tools_in_gateway`、workspace scope、下游客户端是否接收工具 |
| 工具循环不结束 | `max_tool_rounds`、调用 ID、结果是否回传、synthesis guard |
| Claude Code 404 | `ANTHROPIC_BASE_URL` 是否指向 `/anthropic` |
| Codex 没有 function call | `/v1/responses` 路径、adapter、Responses event 格式 |
| 上游收到 `workspace_root` | 检查 routing field scrub，普通和 streaming passthrough 都应移除 |
| 长 harness 被拒绝 | `text_tool_adapter_compact_token_limit`、context compact、fan-out |
| 外部 Provider 用例 skip | 设置 `TEST_UPSTREAM_URL` 和所需凭据再运行 live E2E |

## 证据边界

本地 mock/public-surface smoke 证明 Gateway 路由、协议、工具所有权和副作用；只有带真实 URL/凭据的 live E2E 才证明某个外部 Provider 在当时环境中可用。两类结果不能混写。
