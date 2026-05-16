# Gateway Admin UI、配置、下游 Key 与请求留存

## 1. 当前实现

当前 Gateway 仍是 Python 实现，入口：

```bash
./scripts/mimo_gateway.sh start
```

管理 UI：

```text
http://127.0.0.1:8885/ui
```

默认管理员：

```text
username: admin
password: admin（开发/测试用默认值，生产环境必须通过环境变量 GATEWAY_ADMIN_PASSWORD 修改）
```

默认下游 API Key：

```text
无默认值，必须通过环境变量 GATEWAY_DOWNSTREAM_KEY 设置
```

> **重要**：生产环境必须设置 `GATEWAY_ADMIN_PASSWORD`（管理员密码）和 `GATEWAY_DOWNSTREAM_KEY`（下游 API Key）。开发/测试环境可使用默认值 `admin/admin`，但必须在上线前通过环境变量修改。

---

## 2. 配置文件

默认配置文件：

```text
.gateway_service.json
```

可通过环境变量覆盖路径：

```bash
GATEWAY_CONFIG_PATH=/path/to/gateway-config.json
```

UI 支持配置：

- 多个上游 API profile：base URL / API key / model / protocol
- protocol：`openai_chat` / `openai_responses` / `anthropic_messages` / `openai_compatible`
- 上游路径映射：models / chat completions / responses / messages
- 上游能力勾选：streaming / tool calls / function calls / parallel tool calls / vision / network / web search / JSON schema
- 下游多个 API key，以及每个 key 可访问的协议：models / chat completions / responses / messages / direct tools
- 是否启用 tools / native tools 是否已验证
- 是否用于 coding agent
- 上游 timeout、max input tokens、max output tokens、upstream max concurrency
- 上游能力开关：streaming、识图/vision、网络/web、tool calls、function calls、parallel tool calls、JSON schema
- 上游路由适配：models、chat/completions、responses、messages，可用于非标准兼容服务
- tool mode：`orchestrate` / `passthrough`
- max tool rounds
- gateway max concurrent requests、排队等待超时、tool execution timeout
- workspace root
- 是否允许写入工具
- 是否允许 shell 工具
- 是否保留下游请求和响应
- 是否记录未支持/失败 tools 到 SQLite `gateway_log.sqlite3`
- 是否启用文本工具调用兜底：当上游没有返回原生 `tool_calls` / `tool_use`，但输出 `<function=Tool>` / `<parameter=name>` 标记时，Gateway 会本地执行真实工具并把结果回填继续生成；这不是把文本伪造成协议 tool call。
- 无限上下文/context router：启用、fan-out、max input tokens、chunk tokens、max chunks、max workers；`fanout_max_chunks=0` 表示按内容完整切片、不人为截断
- 分流综合后的质量审查：可要求上游在综合后再进行检查、反思和调整，输出最终结论
- 本地 MCP / connector catalog JSON
- HTTP Actions JSON

配置片段示例：

```json
{
  "upstream": {
    "base_url": "<YOUR_UPSTREAM_BASE_URL>",
    "api_key": "sk-...",
    "model": "mimo-v2.5-pro",
    "timeout_seconds": 60,
    "max_input_tokens": 128000,
    "max_output_tokens": 8192,
    "max_concurrency": 32,
    "paths": {
      "models": "/v1/models",
      "chat_completions": "/v1/chat/completions",
      "responses": "/v1/responses",
      "messages": "/v1/messages"
    },
    "capabilities": {
      "supports_streaming": true,
      "supports_tools": true,
      "supports_function_calls": true,
      "supports_parallel_tool_calls": true,
      "supports_vision": false,
      "supports_network": false,
      "supports_json_schema": true
    }
  },
  "gateway": {
    "tool_mode": "orchestrate",
    "max_concurrent_requests": 32,
    "concurrency_queue_timeout_seconds": 5,
    "record_unsupported_tools": true,
    "text_tool_call_fallback_enabled": true
  },
  "context": {
    "enabled": true,
    "fanout_enabled": true,
    "fanout_max_chunks": 0,
    "fanout_max_workers": 4,
    "quality_review_enabled": true
  }
}
```

---

## 3. 下游 Key

下游客户端包括：

```text
Codex
Claude Code
DeepSeek-TUI
OpenCode
其他 SDK/App
```

它们调用 Gateway 时需要使用 Bearer key：

```bash
curl http://127.0.0.1:8885/v1/chat/completions \
  -H 'authorization: Bearer <GATEWAY_DOWNSTREAM_KEY>' \
  -H 'content-type: application/json' \
  -d '{"model":"m","messages":[{"role":"user","content":"hello"}]}'
```

也支持：

```text
x-api-key: <GATEWAY_DOWNSTREAM_KEY>
```

UI 支持添加多个下游 key，每个 key 记录：

- name
- prefix
- enabled
- created_at
- key_hash

实际密钥只保存 hash，不在 UI 中回显完整 key。

---

## 4. 请求留存

默认日志后端：

```text
gateway_log.sqlite3
```

当前默认使用 SQLite + WAL 存储请求、失败 tool 和统计，避免高频请求下每次 append JSONL / rewrite JSON 造成 IO 瓶颈。旧文件 `.gateway_requests.jsonl`、`.gateway_tool_failures.jsonl`、`.gateway_stats.json` 只作为历史导入和兼容读取，不再默认写入。

请求记录包含：

- request_id
- 时间
- path
- status
- downstream key name
- 请求体
- 响应体
- fake_prompt_tools=false

敏感字段会脱敏：

```text
authorization
api_key
x-api-key
key
token
password
secret
```

用途：

1. 复现下游请求。
2. 分析 Codex / Claude Code / DeepSeek-TUI 的实际调用行为。
3. 回放失败样本。
4. 后续做 Gateway 智力提升、tool 使用优化、自动补 connector。

---

## 5. 调用频次与失败记录

统计存储：

```text
gateway_log.sqlite3 / tool_stats / request_stats*
```

记录：

- 每个 tool 的 calls/success/failure
- failure type 分布
- request by path/status

失败日志存储：

```text
gateway_log.sqlite3 / tool_failures
```

记录：

- `tool_not_found`
- `connector_required`
- `permission_denied`
- `invalid_input`
- `execution_failed`
- `timeout`

UI 会展示最近失败，作为后续去市场搜索 MCP/OpenAPI/action/plugin 支持的入口。

---

## 6. MCP / Marketplace

当前 UI 已有本地 MCP / connector catalog 配置入口，用 JSON 保存到 `.gateway_service.json`：

```json
[
  {
    "name": "github",
    "type": "mcp_stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": ["GITHUB_TOKEN"],
    "pool": true,
    "catalog_ttl": 60
  }
]
```

当前阶段已实现：

- 保存 MCP 配置。
- stdio MCP `initialize`。
- stdio MCP `tools/list`。
- stdio MCP `tools/call`。
- stdio MCP 长连接 session pool，避免每次调用都重启进程。
- `tools/list` catalog cache，默认 TTL 60 秒。
- UI 支持刷新 MCP 连接和工具缓存。
- UI 和 JSON 接口支持 MCP 健康状态查看：`/admin/mcp-health.json`。
- `probe=1` 可触发主动健康探测：`/admin/mcp-health.json?probe=1`。
- MCP `tools/list` / `tools/call` 失败时会自动关闭对应 session、清理 catalog cache，并把 server 标记为 `broken`。
- 下次同一 MCP server 被调用时会重新启动 stdio session，相当于自动 reconnect/restart。
- 将 ready MCP tools 自动合并到 Gateway tools，命名格式为 `mcp__<server>__<tool>`。
- 记录 connector_required / execution_failed 等失败。
- 统计 function/tool 调用频次。

MCP health 字段：

```json
{
  "servers": [
    {
      "name": "github",
      "enabled": true,
      "status": "ready|broken|restarting|unknown",
      "session": "connected|not_connected",
      "cache": "hit|miss",
      "tool_count": 12,
      "cached_tool_count": 12,
      "detail": ""
    }
  ]
}
```

状态含义：

- `ready`：最近一次 `tools/list` 或 `tools/call` 成功。
- `restarting`：Gateway 已因失败关闭旧 session 并清理缓存，下一次会重新拉起。
- `broken`：主动 probe 或实际调用失败，需要检查 command/env/auth/cwd。
- `unknown`：尚未调用过，也没有缓存状态。

下一阶段：

- MCP SSE/HTTP transport。
- Marketplace 自动搜索和安装。
- 失败工具自动搜索 marketplace/candidate connector。

---

## 7. HTTP Actions

HTTP Action 是当前已落地的第二类真实 executor。它适合把内部已有的 HTTP 服务、自动化服务、action server、轻量 function service 直接包装成 tool/function call。

配置保存在 `.gateway_service.json`：

```json
{
  "http_actions": {
    "enabled": true,
    "actions": [
      {
        "name": "lookup_user",
        "description": "Lookup user by id",
        "method": "POST",
        "url": "http://127.0.0.1:9000/lookup",
        "headers": {
          "authorization": "${LOOKUP_TOKEN}"
        },
        "input_schema": {
          "type": "object",
          "properties": {
            "id": {"type": "string"}
          },
          "required": ["id"]
        },
        "timeout": 30,
        "max_bytes": 200000,
        "enabled": true
      }
    ]
  }
}
```

执行规则：

- `name` 会作为 tool/function 名直接暴露给 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`。
- `POST` / `PUT` / `PATCH`：把 tool arguments 作为 JSON body 发送。
- `GET` / `DELETE`：把 tool arguments 转成 query string。
- `headers` 支持 `${ENV_NAME}` 读取环境变量，避免把 token 写死在配置文件。
- HTTP 4xx/5xx、连接失败、非法 URL 都会作为真实 tool failure 回填给上游 AI，并写入失败日志。

管理接口：

```text
/admin/tools.json
/admin/http-actions.json
```

这不是 prompt fake：模型必须返回协议级 tool/function call，Gateway 才会执行 HTTP action，并把真实 HTTP 结果作为 tool result 回填。

---

## 8. 稳定性策略

- 下游访问必须带 key。
- Admin UI 使用 Basic Auth。
- 写入和 shell 默认关闭。
- 优先执行协议级 tool call；仅当上游退化为 Claude-Code-like 文本 `<function=...>` 标记时，Gateway 才会把它作为 fallback 指令执行本地真实工具，并记录为 `gateway_local_tool_fallback`。
- 失败写入日志，后续持续迭代。
- 请求和响应留存，方便复现和分析。
