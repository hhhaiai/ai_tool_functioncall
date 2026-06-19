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
- protocol：`openai_chat` / `openai_responses` / `anthropic_messages`
- 上游路径映射：models / chat completions / responses / messages
- 上游能力勾选：streaming / tool calls / function calls / parallel tool calls / vision / network / web search / JSON schema
- 下游多个 API key，以及每个 key 可访问的协议：models / chat completions / responses / messages / direct tools
- 是否启用 tools / native tools 是否已验证
- 是否用于 coding agent
- 上游 timeout、max input tokens、max output tokens、upstream max concurrency
- 上游能力开关：streaming、识图/vision、网络/web、tool calls、function calls、parallel tool calls、JSON schema
- 上游路由适配：models、chat/completions、responses、messages，可用于非标准兼容服务
- tool mode：`orchestrate` / `native_passthrough` / `proxy`（兼容旧值 `passthrough`）
- max tool rounds
- gateway max concurrent requests、排队等待超时、tool execution timeout
- text tool adapter compact token limit：弱上游文本工具适配前压缩 Claude Code/Codex 大 harness，默认 48000
- local planner：对分析/审查和点名文件读取请求，默认合成下游用户侧工具请求，避免弱上游只说“我将读取文件”但不发 tool call；显式本地代理模式才由 Gateway 读文件/目录/符号并注入证据。
- workspace root
- 是否允许写入工具
- 是否允许 shell 工具
- 是否保留下游请求和响应
- 是否记录未支持/失败 tools 到 SQLite `gateway_log.sqlite3`
- 是否启用文本工具调用兜底：当上游没有返回原生 `tool_calls` / `tool_use`，但输出 `<function=Tool>` / `<parameter=name>` 标记时，Gateway 会解析并按工具归属处理；gateway-owned 工具执行并回填，用户侧工具返回下游原生 tool request。
- 无限上下文/context router：启用、fan-out、max input tokens、chunk tokens、max chunks、max workers；`fanout_max_chunks=0` 表示按内容完整切片、不人为截断
- 分流综合后的质量审查：可要求上游在综合后再进行检查、反思和调整，输出最终结论
- 本地 MCP / connector catalog JSON
- HTTP Actions JSON
- Admin 数字字段解析契约：上游/gateway/context/client-config 中的数字项会先按提交值解析；字段缺失或空字符串时保留已有配置；已有配置也不存在时才使用默认值；非法数字返回结构化 400 且不保存任何本次变更。

### 2.1 Admin 数字字段逐行保存链路

本轮逐行检查的核心路径是 `src/gateway_http_handler.py` 的 `GatewayHandler.do_POST()`：

1. `path = self.path.split("?", 1)[0]`：只用 path 部分做路由，query 不参与 Admin 写操作分支。
2. Admin 写接口集合命中后先 `_check_admin()`，Basic Auth 不通过直接 401，不读取表单。
3. `cfg = load_config()` 读取当前 `.gateway_service.json`；坏 JSON 会 fail closed 返回结构化错误，不静默回默认配置。
4. `_check_admin_origin(self, cfg)` 在读取/保存表单前校验浏览器 `Origin` / `Referer`，跨源或畸形来源返回 403。无来源头的 CLI/脚本请求保持兼容。
5. `_read_form(self)` 统一读取 urlencoded 或 JSON 表单；读取前受 `gateway.max_request_body_bytes` 限制。
6. `/admin/config` 先调用 `_profile_from_admin_form()` 解析上游 profile；其中 `upstream_timeout_seconds` 用 float，`upstream_max_input_tokens` / `upstream_max_output_tokens` / `upstream_max_concurrency` 用 int。非法值会在保存前转为 400。
7. `/admin/config` 再解析 gateway 数字项：`max_tool_rounds`、`max_concurrent_requests`、`text_tool_adapter_compact_token_limit` 为 int；`concurrency_queue_timeout_seconds`、`tool_execution_timeout_seconds` 为 float。
8. `/admin/config` 最后解析 context 数字项：`context_max_input_tokens`、`context_fanout_chunk_tokens`、`context_fanout_max_chunks`、`context_fanout_max_workers` 均为 int。
9. `/admin/client-config` 解析客户端片段数字项：`client_context_window`、`client_auto_compact_token_limit`、`client_output_token_limit` 均为 int。
10. `/admin/upstream-profile` 保存单个 profile 时复用同一套上游数字解析；非法数字同样返回 400，不新增/覆盖 profile。
11. 所有数字解析都走 `gateway_config._admin_form_int()` / `_admin_form_float()`：提交值优先，缺失/空值保留旧配置，再 fallback 到默认值。
12. 只有全部字段解析成功后才调用 `save_config(cfg)`；因此非法数字不会产生“部分字段已写入”的配置污染。
13. `gateway.max_tool_rounds` 保存后会被非流式 `run_tool_orchestration()` 和流式 `run_streaming_orchestration()` 使用；优先级是 `GATEWAY_MAX_TOOL_ROUNDS` 环境变量 > `gateway.max_tool_rounds` 配置 > 默认 `5`。
14. `gateway.max_concurrent_requests` / `concurrency_queue_timeout_seconds` 保存后会被 HTTP API 入口统一执行；`/v1/*`、direct tools、token count 和 `/v1/models` 都会先获取并发槽位，超过上限返回结构化 429，并在请求完成或异常时释放槽位。

配置片段示例：

```json
{
  "upstream": {
    "base_url": "<YOUR_TEST_UPSTREAM_BASE_URL>",
    "api_key": "<YOUR_UPSTREAM_API_KEY>",
    "model": "mimo-v2.5-pro",
    "tools_enabled": "adapter",
    "timeout_seconds": 60,
    "max_input_tokens": 1048576,
    "max_output_tokens": 131072,
    "max_concurrency": 32,
    "paths": {
      "models": "/v1/models",
      "chat_completions": "/v1/chat/completions",
      "responses": "/v1/responses",
      "messages": "/v1/messages"
    },
    "capabilities": {
      "supports_streaming": true,
      "supports_tools": false,
      "supports_function_calls": false,
      "supports_parallel_tool_calls": false,
      "supports_vision": false,
      "supports_network": false,
      "supports_web_search": false,
      "supports_json_schema": true
    }
  },
  "gateway": {
    "tool_mode": "orchestrate",
    "max_concurrent_requests": 32,
    "concurrency_queue_timeout_seconds": 5,
    "record_unsupported_tools": true,
    "text_tool_call_fallback_enabled": true,
    "text_tool_adapter_compact_token_limit": 48000
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

### 2.2 上游模型自动获取

新版 Admin UI 顶部为 **Gateway Control Center**，上游配置卡片包含 **Capability Matrix / 能力矩阵** 与 **Fetch Models /v1/models** 按钮。点击模型按钮后浏览器调用：

```text
GET /admin/upstream-models.json   # 只使用已保存 active profile，忽略临时 query 覆盖
POST /admin/upstream-models.json  # 使用表单临时配置，api_key 放在 body 中
```

后端会使用当前表单或已保存 active profile 的这些字段请求真实上游：

- `base_url`
- `api_key`（仅 POST body；GET 不接受 query 中的临时 key，避免 URL 日志泄漏）
- `base_url` / `path_models` 临时覆盖也仅 POST body 生效；GET 忽略 query 覆盖，避免带保存的上游 Authorization header 请求非预期 URL。
- `protocol`（`openai_chat` / `openai_responses` 使用 `Authorization: Bearer`，`anthropic_messages` 使用 `x-api-key`）
- `path_models` / `upstream.paths.models`，默认 `/v1/models`

返回形态：

```json
{
  "ok": true,
  "active_model": "mimo-v2.5-pro",
  "base_url": "http://127.0.0.1:8885",
  "path": "/v1/models",
  "models": ["mimo-v2.5-pro"],
  "raw": {}
}
```

模型 ID 会从常见 OpenAI-compatible 形态中提取：`data[].id`、`models[]`、`items[]`，并去重排序。

### 2.3 上游 tools / vision capability 运行语义

Admin UI 保存的能力开关不是装饰字段，会影响运行时。真实测试 Mimo 类上游当前应保存为 `supports_tools=false` / `supports_function_calls=false`，由 Gateway adapter/orchestrate 做协议适配；真实地址只放本地 ignored 配置或环境变量。原因是：虽然该上游 `/v1/messages` forced tool_choice 探针可返回 Anthropic `tool_use`，但它没有 `/anthropic` 别名和 direct tools/functions endpoint，且 `/v1/responses` forced tool probe 未返回 Codex 所需 `function_call`；跨 Claude Code + Codex 的稳定默认值仍应是 adapter。


- `tools_enabled=auto` 且 `supports_tools=false` 或 `supports_function_calls=false`：Gateway 不向上游发送原生 `tools` schema，改为注入文本工具适配说明，并解析 `<function=Tool>` / `<parameter=name>`。解析出的调用会先做归属判断：gateway-owned 工具在 Gateway 真执行，用户侧文件/shell/GUI/local-agent/Skill 工具返回给下游客户端执行。
- `tools_enabled=native`：按原生 tools 发送。
- `tools_enabled=native_only` 且能力关闭：请求 fail-fast，避免 Claude Code/Codex 误以为上游支持真实 tool protocol。
- `supports_vision`、`supports_streaming`、`supports_json_schema` 等能力会在 UI 和配置中明确展示，供路由/运维判断。
- 当走文本工具适配且请求超过 `gateway.text_tool_adapter_compact_token_limit` 时，Gateway 会先压缩 Claude Code/Codex 的大 system/reminder/tool schema harness，再注入紧凑工具说明，避免弱上游返回 provider 级 `too long`。

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
- `GET` / `DELETE`：把 tool arguments 追加为 query string；`POST` / `PUT` / `PATCH` 默认发送 JSON body。
- `headers` 支持 `${ENV_NAME}` 读取环境变量，避免把 token 写死在配置文件；query 参数只做安全字符串化，bool 会稳定编码为 `true` / `false`，不会展开模型传入的 `${ENV}`。
- `url` 必须是绝对 `http(s)` URL；`file://` 等非 HTTP scheme 会作为 `invalid_input` 失败。
- `max_bytes` 同时限制成功响应和错误响应体，默认 1MB；超限返回 `response_too_large`，避免把巨大 action 响应写入上下文/日志。
- HTTP 4xx/5xx、连接失败、非法 URL、响应超限都会作为真实 tool failure 回填给上游 AI，并写入失败日志。
- HTTP Action 默认不重试，避免 POST/PUT/PATCH 这类外部副作用被重复触发；只有 action 显式配置 `max_retries` 时才重试。

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
- 优先处理协议级 tool call；仅当上游退化为 Claude-Code-like 文本 `<function=...>` 标记时，Gateway 才把它作为 fallback 指令解析。解析后仍按工具归属分流：HTTP Action/MCP/网络/纯函数等 Gateway-owned 工具执行并记录结果；Read/Bash/Skill 等用户侧工具返回下游原生 tool request。
- 失败写入日志，后续持续迭代。
- 请求和响应留存，方便复现和分析。


### 2.8 workspace_root 优先级

工具读写根目录按以下顺序解析：

1. 请求体中的 `workspace_root` / `gateway_workspace`。
2. 下游客户端项目目录信号：Claude Code 的 `Primary working directory` / `Worktree`，Codex Responses 的 `<environment_context><cwd>`，以及请求 metadata 中的 `projectDir` / `cwd` 等字段。
3. `GATEWAY_WORKSPACE_ROOT` 环境变量。
4. Admin UI / 配置文件显式保存的 `gateway.workspace_root`。
5. 匿名隔离空间（不会回退 Gateway 服务启动目录）。

这样可以同时保证：Gateway 作为中游服务启动在自身仓库时，不会把服务目录误当成用户项目目录；`/Users/sanbo/Desktop/PersonalAIBrain/.traces` 这类项目级路径会按下游项目根解析；测试或运维显式设置到其他目录时仍能立即生效；缺失 workspace 的普通对话会进入匿名隔离空间而不是服务 cwd。

Skills / 插件目录也使用同一项目根：先扫描当前下游项目的 `.codex/skills`、`.claude/skills`、`.opencode/skills`、`.agents/skills`、`skills/`，再扫描项目内 `.codex/plugins` / `.claude/plugins` / `.opencode/plugins` / `plugins` 的 manifest 声明 skills，最后才加载用户全局 skills 和显式 `GATEWAY_SKILLS_DIRS`。项目内插件声明的 skills 路径必须仍在项目根内，避免服务 cwd 或其它项目插件串入当前请求。
