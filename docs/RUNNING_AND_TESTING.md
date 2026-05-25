# Gateway 部署与运行指南

本文档涵盖从零开始部署、配置、启动和验证 `AI Tool FunctionCall Gateway` 的完整流程。

**特性**：纯 Python 标准库实现，无第三方依赖，支持 macOS / Linux / Windows (WSL)。

---

## 目录

1. [环境要求](#1-环境要求)
2. [快速开始](#2-快速开始)
3. [配置说明](#3-配置说明)
4. [启动服务](#4-启动服务)
5. [验证部署](#5-验证部署)
6. [客户端接入](#6-客户端接入)
7. [生产环境部署](#7-生产环境部署)
8. [常见问题](#8-常见问题)

---

## 1. 环境要求

| 依赖 | 版本 | 必需 | 说明 |
|------|------|------|------|
| Python | 3.10+ | ✅ | 核心运行时 |
| sqlite3 | - | ✅ | Python 内置 |
| curl | - | ✅ | 健康检查 |
| screen / nohup | - | 可选 | 后台运行方式 |

**可选依赖**（用于 `view_image` / `computer_use` / GUI 输入工具）：

```bash
# 本地图片解析 / 截图保存
pip install pillow

# Linux/Windows 截图、鼠标、键盘后端
pip install pyautogui

# macOS 截图、鼠标、键盘后端
pip install pyobjc-framework-Quartz
```

---

## 2. 快速开始

### 2.1 克隆代码

```bash
git clone <repository-url> /opt/ai-tool-functioncall
cd /opt/ai-tool-functioncall
```

### 2.2 创建配置文件

```bash
# 从模板创建本地配置
cp gateway.config.json .gateway_service.json
```

### 2.3 编辑配置

```bash
vi .gateway_service.json
```

**最小配置**（只需修改这几项）：

```json
{
  "upstream": {
    "base_url": "<YOUR_TEST_UPSTREAM_BASE_URL>",
    "api_key": "<YOUR_UPSTREAM_API_KEY>",
    "model": "mimo-v2.5-pro"
  },
  "gateway": {
    "workspace_root": "./workspace",
    "client_snippet_api_key": "your-gateway-api-key"
  },
  "admin": {
    "password": "your-secure-password"
  }
}
```

### 2.4 启动服务

```bash
./scripts/mimo_gateway.sh start
```

### 2.5 验证

```bash
curl http://127.0.0.1:8885/healthz
```

预期输出：

```json
{"ok": true, "mode": "orchestrate", "fake_prompt_tools": false}
```

---

## 3. 配置说明

### 3.1 配置文件位置

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | 环境变量 | 最高优先级 |
| 2 | `.gateway_service.json` | 本地配置（不入 git） |
| 3 | `gateway.config.json` | 模板配置 |
| 4 | 代码默认值 | 最低优先级 |

### 3.2 完整配置项

```json
{
  "admin": {
    "username": "admin",
    "password": "admin",
    "password_hash": ""
  },
  "upstream": {
    "base_url": "<YOUR_TEST_UPSTREAM_BASE_URL>",
    "api_key": "<YOUR_UPSTREAM_API_KEY>",
    "model": "mimo-v2.5-pro",
    "protocol": "openai_chat",
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
    "workspace_root": "./workspace",
    "tool_mode": "orchestrate",
    "allow_write_tools": false,
    "allow_shell_tools": false,
    "max_tool_rounds": 5,
    "tool_execution_timeout_seconds": 60,
    "max_concurrent_requests": 32,
    "request_logging": true,
    "logging_backend": "sqlite",
    "public_base_url": "http://127.0.0.1:8885",
    "client_snippet_api_key": "your-gateway-key",
    "downstream_model_alias": "",
    "client_context_window": 1048576,
    "client_auto_compact_token_limit": 943718,
    "client_output_token_limit": 131072,
    "text_tool_adapter_compact_token_limit": 48000,
    "local_planner_enabled": true,
    "local_planner_max_files": 24
  },
  "context": {
    "enabled": true,
    "max_input_tokens": 1048576,
    "fanout_enabled": true,
    "fanout_chunk_tokens": 120000,
    "memory_enabled": true
  },
  "downstream_keys": [],
  "mcp": {
    "servers": [],
    "marketplace_enabled": true
  },
  "http_actions": {
    "enabled": true,
    "actions": []
  }
}
```

### 3.3 关键配置项说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `admin.password` | admin | 管理员密码；加载/保存时会归一化为 `password_hash`，生产环境必须改为强密码或使用 `GATEWAY_ADMIN_PASSWORD` |
| `upstream.base_url` | 空（模板）/ 本地配置 | 上游 LLM API 地址；真实测试地址只放 `.gateway_service.json`、`.env` 或运行时环境变量，不提交；当前 Mimo 类上游支持 chat/responses/messages，但无 `/anthropic` 别名和 direct tools endpoint |
| `upstream.api_key` | - | 上游 API Key |
| `upstream.model` | `mimo-v2.5-pro`（模板） | 默认模型名称 |
| `upstream.protocol` | openai_chat | 上游协议类型 |
| `upstream.max_input_tokens` / `max_output_tokens` | 1048576 / 131072（Mimo 模板） | 上游上下文/输出预算声明；客户端片段按 1M 同步 |
| `upstream.tools_enabled` | adapter（Mimo 模板） | 工具模式：`auto` 按能力自动选择；`native` 发送原生 tools；`native_only` 能力不足即失败；`adapter`/`text_only`/`off` 走 Gateway 本地真实工具文本适配 |
| `upstream.paths.models` | `/v1/models` | Admin UI 模型自动获取和 `/v1/models` 转发使用的上游路径 |
| `upstream.capabilities.supports_tools` / `supports_function_calls` | false（Mimo 模板） | 上游是否原生支持当前客户端所需 tools/function calls；Mimo `/v1/messages` forced probe 可返回 `tool_use`，但 `/v1/responses` function_call 未证实，Claude Code/Codex 默认由 Gateway 本地 runtime 执行 |
| `upstream.capabilities.supports_vision` | false | 上游是否支持图片/截图/识图输入；在 Admin UI 明确展示和保存 |
| `upstream.capabilities.supports_network` | false | 上游模型是否具备联网能力；在 Admin UI 明确展示和保存 |
| `upstream.capabilities.supports_web_search` | false | 上游模型是否支持 web search；在 Admin UI 明确展示和保存 |
| `gateway.workspace_root` | `./workspace`（模板）/ 当前目录（无配置时） | 工具读写的兜底根目录；当前请求的显式 `workspace_root` / `gateway_workspace` 优先，其次自动识别 Claude Code / Codex 下游项目目录，再其次才是非默认 `GATEWAY_WORKSPACE_ROOT`、保存配置和默认当前目录 |
| `gateway.tool_mode` | orchestrate | 工具模式：`orchestrate` / `native_passthrough` / `proxy`；兼容旧值 `passthrough` |
| `gateway.allow_write_tools` | false | 是否允许文件写入 |
| `gateway.allow_shell_tools` | false | 是否允许 Shell 执行 |
| `gateway.client_snippet_api_key` | - | 客户端连接 Gateway 的 API Key；保存配置时会自动同步为可认证的 downstream key |
| `gateway.client_context_window` | 1048576 | 下游 Claude Code/Codex 配置片段中的上下文窗口；Mimo 按 1M 配置 |
| `gateway.max_tool_rounds` | 5 | orchestrate 模式最大工具调用轮数；运行时优先使用 `GATEWAY_MAX_TOOL_ROUNDS` 环境变量，其次使用 Admin/配置文件保存值 |
| `gateway.max_concurrent_requests` | 32 | Gateway 下游 API 请求并发上限；HTTP 入口会先获取并发槽位，超过上限按 `concurrency_queue_timeout_seconds` 等待后返回 429 |
| `gateway.concurrency_queue_timeout_seconds` | 5.0 | 并发槽位排队等待时间；超时返回 429 |
| `gateway.tool_execution_timeout_seconds` | 60.0 | 单次工具执行超时 |
| `gateway.max_request_body_bytes` | 67108864 | HTTP POST 请求体读取前上限；超限返回 413，避免大请求先占用内存 |
| `gateway.max_log_payload_chars` | 200000 | 单个 request/response 日志 payload 与 tool failure 内容截断上限；先遮盖敏感字段再截断 |
| `gateway.text_tool_adapter_compact_token_limit` | 48000 | 弱上游文本工具适配前的压缩阈值上限；实际阈值动态计算为 `max(8000, min(upstream.max_input_tokens * 0.45, 此值))`；设为 0 可关闭 |
| `gateway.local_planner_enabled` | true | 对分析/审查请求以及点名 `read/show/cat/open/查看/读取` 文件路径的请求，先用 Gateway 本地真实读文件/目录/符号工具注入证据；弱上游不发 tool call 时也能稳定完成本地文件读取 smoke；文件路径按当前下游项目根隔离 |
| `context.max_input_tokens` | 1048576 | Mimo 1M 上下文阈值；超过此值触发上下文压缩/扇出 |

### 3.4 环境变量对照表

| 环境变量 | 配置路径 | 说明 |
|----------|----------|------|
| `UPSTREAM_BASE_URL` | upstream.base_url | 上游 API 地址 |
| `UPSTREAM_API_KEY` | upstream.api_key | 上游 API Key |
| `UPSTREAM_MODEL` | upstream.model | 默认模型 |
| `UPSTREAM_MAX_INPUT_TOKENS` / `UPSTREAM_MAX_OUTPUT_TOKENS` | upstream token limits | Mimo 模板为 `1048576` / `131072` |
| `GATEWAY_TOOLS_ENABLED` | upstream.tools_enabled | Mimo 跨 Claude Code/Codex 稳定接入时设为 `adapter` |
| `UPSTREAM_SUPPORTS_TOOLS` / `UPSTREAM_SUPPORTS_FUNCTION_CALLS` | upstream.capabilities | Mimo Messages `tool_use` 仅部分证实；Codex Responses function_call 未证实，默认均设为 `0` |
| `GATEWAY_UPSTREAM_PROTOCOL` | upstream.protocol | 上游协议类型，优先于 legacy `UPSTREAM_PROTOCOL` |
| `UPSTREAM_PROTOCOL` | upstream.protocol | 兼容旧环境变量，未设置 `GATEWAY_UPSTREAM_PROTOCOL` 时生效 |
| `GATEWAY_DOWNSTREAM_KEY` | downstream key + gateway.client_snippet_api_key | 下游 API Key；环境变量会同时用于认证和客户端片段 |
| `GATEWAY_ADMIN_PASSWORD` | admin.password | 管理员密码 |
| `GATEWAY_WORKSPACE_ROOT` | gateway.workspace_root | 显式设置为非当前目录时优先于保存配置，但仍低于请求体 root 和下游客户端项目目录；启动脚本默认导出的 `$PWD` 只作为兜底，不会压过 UI/配置保存的 workspace_root |
| `GATEWAY_SKILLS_DIRS` | Skill tool search path | 额外 skills 目录，使用 `:` 分隔；加载顺序在当前下游项目 skills、项目内插件 skills、用户全局 skills 之后 |
| `GATEWAY_PORT` | - | 监听端口（默认 8885） |
| `GATEWAY_HOST` | - | 监听地址（默认 0.0.0.0） |
| `GATEWAY_SQLITE_LOG_PATH` | gateway.sqlite_log_path | SQLite 请求/工具/记忆日志路径 |
| `GATEWAY_MAX_TOOL_ROUNDS` | gateway.max_tool_rounds | 最大工具调用轮数；设置后优先于配置文件/Admin UI 保存值 |
| `GATEWAY_MAX_REQUEST_BODY_BYTES` | gateway.max_request_body_bytes | POST 请求体读取前字节上限，默认 64MB，超限返回 413 |
| `GATEWAY_MAX_LOG_PAYLOAD_CHARS` | gateway.max_log_payload_chars | 单个 request/response 日志 payload 与 tool failure 内容字符上限，默认 200000 |
| `GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT` | gateway.text_tool_adapter_compact_token_limit | 文本工具适配前的压缩阈值上限，默认 48000；实际阈值动态计算；设为 0 可关闭 |
| `GATEWAY_CONTEXT_MAX_INPUT_TOKENS` | context.max_input_tokens | Mimo 1M 模板为 `1048576` |
| `GATEWAY_CLIENT_CONTEXT_WINDOW` | gateway.client_context_window | Claude Code/Codex 客户端上下文窗口，Mimo 模板为 `1048576` |

配置文件存在但 JSON 损坏或根节点不是对象时，Gateway 会 fail closed：Admin/API 请求返回结构化 500 `invalid gateway config`，不会静默回退到默认 `admin/admin` 或无下游鉴权。修复方式是恢复有效 `.gateway_service.json`，而不是依赖代码默认值。

HTTP Action 执行遵循真实 executor 契约：`GET` / `DELETE` 使用 query，`POST` / `PUT` / `PATCH` 使用 JSON body，`headers` 可通过 `${ENV_NAME}` 注入环境变量，`max_bytes` 默认限制响应体为 1MB；HTTP/URL/响应超限错误会记录为 tool failure，且默认不重试以避免外部副作用重复执行。

Gateway 会在读取前限制 HTTP POST 请求体大小：`gateway.max_request_body_bytes` / `GATEWAY_MAX_REQUEST_BODY_BYTES` 默认 64MB，超限返回结构化 413，避免 API 请求或 Admin form 在进入上下文压缩/业务校验前占用过多内存。配置了 downstream key 时，受保护 `/v1/*` 和 direct-tool POST 会先校验 key，再读取/解析 JSON body；未授权 malformed/oversized body 仍返回 401。

请求/响应日志、tool failure 内容和 Admin 配置展示会遮盖常见敏感字段，包括 `Authorization`、`X-API-Key`、`Cookie`、token、secret、password、`key_hash` 等；`must_change_password` 等非敏感状态字段会保留原值。请求/响应日志与 tool failure 内容在遮盖之后会按 `gateway.max_log_payload_chars` / `GATEWAY_MAX_LOG_PAYLOAD_CHARS` 截断，避免长 prompt、大响应或失败详情导致 SQLite/JSONL 日志膨胀。

Admin 写操作会校验浏览器 `Origin` / `Referer`：跨源请求返回 403，畸形来源 fail closed；同源请求和无来源头的 CLI/脚本请求保持可用。反向代理部署时请正确传递 `Host` / `X-Forwarded-Host` / `X-Forwarded-Proto`，或配置 `gateway.public_base_url`。

Admin 表单数字字段是 fail-closed 语义：`/admin/config`、`/admin/upstream-profile`、`/admin/client-config` 中的数字项如果提交非法值，会返回结构化 400 `invalid numeric field: <field>`，并且不会保存本次请求里的任何部分变更；字段缺失或空字符串时保留已有配置，已有配置不存在时才使用默认值。这覆盖上游 timeout/token/concurrency、gateway tool rounds/concurrency/timeout、context fanout 参数和 client snippet token limits。

`gateway.text_tool_adapter_compact_token_limit` 是弱上游文本工具适配前的压缩阈值上限。实际阈值动态计算为 `max(8000, min(upstream.max_input_tokens * 0.45, config_cap))`：小上游保底 8000 tokens，中上游按 45% 比例缩放，大上游由 config_cap 封顶（默认 48000）。设为 0 可关闭该专项压缩。

---

## 4. 启动服务

### 4.1 启动方式

```bash
# 方式 1：screen 后台运行（默认，推荐）
./scripts/mimo_gateway.sh start

# 方式 2：nohup 后台运行
GATEWAY_START_METHOD=nohup ./scripts/mimo_gateway.sh start

# 方式 3：前台运行（调试用）
./scripts/mimo_gateway.sh foreground

# 方式 4：launchd（仅 macOS）
GATEWAY_START_METHOD=launchd ./scripts/mimo_gateway.sh start
```

### 4.2 服务管理

```bash
# 查看状态
./scripts/mimo_gateway.sh status

# 停止服务
./scripts/mimo_gateway.sh stop

# 重启服务
./scripts/mimo_gateway.sh start

# 查看日志
tail -f .gateway_runtime/gateway-8885.log
```

### 4.3 自定义端口

```bash
# 方式 1：环境变量
GATEWAY_PORT=9000 ./scripts/mimo_gateway.sh start

# 方式 2：修改配置
export GATEWAY_PORT=9000
./scripts/mimo_gateway.sh start
```

---

## 5. 验证部署

### 5.1 健康检查

```bash
curl http://127.0.0.1:8885/healthz
```

### 5.2 Admin UI

```bash
# 浏览器访问
open http://127.0.0.1:8885/ui

# 命令行验证
curl -u admin:admin http://127.0.0.1:8885/ui
```

### 5.3 测试 API 调用

```bash
# OpenAI 兼容接口
curl http://127.0.0.1:8885/v1/chat/completions \
  -H "Authorization: Bearer your-gateway-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# 查看可用模型
curl http://127.0.0.1:8885/v1/models \
  -H "Authorization: Bearer your-gateway-api-key"
```

### 5.4 运行测试套件

```bash
# 运行全部测试
python3 -m pytest -q

# 运行集成测试
python3 tests/integration/smoke_gateway_tools.py
```

### 5.5 当前稳定性 smoke（临时端口）

不想影响本机 8885 服务时，可用临时配置和端口启动真实进程：

```bash
TMPDIR=$(mktemp -d)
GATEWAY_CONFIG_PATH="$TMPDIR/config.json" \
GATEWAY_SQLITE_LOG_PATH="$TMPDIR/gateway_log.sqlite3" \
GATEWAY_DOWNSTREAM_KEY=smoke-key \
GATEWAY_ADMIN_PASSWORD=admin \
python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8899

# 另一个终端验证
curl -fsS http://127.0.0.1:8899/healthz
curl -fsS -u admin:admin http://127.0.0.1:8899/ui >/dev/null
curl -fsS -u admin:admin http://127.0.0.1:8899/client-config.json >/dev/null
# /v1/models 需要 UPSTREAM_BASE_URL 指向可访问上游；未配置上游时应跳过该项或预期返回上游配置错误。
curl -fsS -H 'authorization: Bearer smoke-key' http://127.0.0.1:8899/v1/models >/dev/null
curl -fsS -H 'authorization: Bearer smoke-key' -H 'content-type: application/json' \
  -d '{"tool":"calculator","arguments":{"expression":"6*7"}}' \
  http://127.0.0.1:8899/v1/tools/call
```

本轮稳定性验证覆盖 Admin 数字字段错误请求：非法数字应返回 400，缺失字段应保留旧配置，`gateway.max_tool_rounds` 应实际限制非流式/流式工具循环。

项目级验证脚本也纳入 `verify`。默认会验证 Gateway 自身的项目根隔离，若本机没有 Claude/Codex CLI 会跳过对应 CLI 子项；设置 `GATEWAY_VERIFY_REQUIRE_CLI=1` 时 Claude Code CLI 与 Codex CLI 也必须通过：

```bash
GATEWAY_VERIFY_MODEL_REQUESTS=0 GATEWAY_VERIFY_DIRECT_REQUESTS=24 GATEWAY_VERIFY_WORKERS=8 GATEWAY_VERIFY_REQUIRE_CLI=1 ./scripts/mimo_gateway.sh verify
# unittest tests OK
# tool acceptance OK
# security/auth guardrails OK
# 24-request concurrency/performance smoke OK
# Claude/Codex project-scope smoke OK
```

也可以单独复跑项目根隔离 smoke：

```bash
python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# 覆盖 direct Skills、项目插件 skill、/v1/functions/call、相对/绝对 .traces、Memory 项目根隔离、
# Anthropic streaming/Claude Code Primary working directory、
# Responses streaming/Codex <environment_context><cwd>，以及真实 Claude/Codex CLI。
```

如果要本地搭建“真实上游形态”的测试服务，但不想把真实公网测试地址写入代码，可运行内置 mock 上游：

```bash
python3 scripts/mock_openai_upstream.py --port 9001 --model mimo-v2.5-pro
UPSTREAM_BASE_URL=http://127.0.0.1:9001 \
GATEWAY_DOWNSTREAM_KEY=local-test-key \
GATEWAY_ADMIN_PASSWORD=admin \
./scripts/mimo_gateway.sh start
```

这个 mock 提供 `/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages`，并刻意让 `/anthropic/*`、`/v1/tools/call`、`/v1/functions/call` 返回 404，用来复现“Mimo 类弱上游 + Gateway adapter/orchestrate 补齐工具能力”的接入形态。

### 5.6 当前 Mimo 上游 + Gateway adapter 复验记录

2026-05-25 的结论是：真实测试 Mimo OpenAI-compatible 上游可用（地址只在本地 ignored 配置/环境变量中保存）；它支持 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`，并且 `/v1/messages` forced tool_choice 探针可返回 Anthropic `tool_use`。但它没有 `/anthropic` 兼容别名、没有 direct tools/functions runtime endpoint，且 `/v1/responses` forced tool probe 未返回 Codex 需要的 `function_call`。为了同时稳定支持 Claude Code + Codex，不要让客户端直连该上游；应让客户端连接本 Gateway：

```bash
# Gateway 本地真实工具 runtime 验活；不依赖上游 tool endpoint
curl -fsS http://127.0.0.1:8885/v1/tools/call \
  -H 'Authorization: Bearer <your-gateway-key>' \
  -H 'Content-Type: application/json' \
  -d '{"tool":"calc","arguments":{"expr":"2+2"}}'

# Claude Code 走 Gateway /anthropic，不直连 Mimo
export ANTHROPIC_BASE_URL=http://127.0.0.1:8885/anthropic
export ANTHROPIC_AUTH_TOKEN=<your-gateway-key>

# Codex 走 Gateway /v1，建议 wire_api=responses
export OPENAI_BASE_URL=http://127.0.0.1:8885/v1
export OPENAI_API_KEY=<your-gateway-key>
```

已复验/回归覆盖：

```text
remote /v1/models / chat / responses / messages     OK
remote /anthropic/v1/messages                       404（上游无该别名）
remote /v1/tools/call / /v1/functions/call          404（上游无工具端点）
remote /v1/messages forced tool_choice              OK，返回 Anthropic tool_use
remote /v1/responses forced tool probe              未返回 function_call，仅 reasoning/message
Gateway /v1/tools/call Bash/calculator              OK，本地真实执行
Gateway /v1/functions/call project trace            OK，按下游项目根读取 .traces，不串到服务根
Gateway /anthropic/v1/messages streaming Read/Bash/Skill OK，adapter/orchestrate 确定性本地工具分支
Anthropic SSE tool_use.input -> input_json_delta     OK，兼容 Claude Code streaming parser
Gateway /v1/responses streaming Bash                OK，Responses SSE 含 output_item/content_part/done，兼容 Codex parser
Gateway streaming passthrough internal fields       OK，转发上游前剥离 workspace/project 路由字段
Claude Code Primary working directory               OK，优先于旧摘要 Worktree，工具/Skills/项目级 .traces 按下游项目根隔离
Codex Responses <environment_context><cwd>           OK，Skills 和工具路径按 Codex 项目根隔离
Gateway Skill/list_skills/read_skill                 OK，项目 `.codex/.claude/.opencode/.agents/skills`、`skills/`、项目内插件 skills、全局 skills、GATEWAY_SKILLS_DIRS
Gateway Memory/RecallMemory                         OK，默认只列出当前下游项目根，服务目录记忆不串入项目
Live Claude Code CLI + Codex CLI project smoke       OK，可复跑 `tests/integration/project_scope_cli_smoke.py --require-claude --require-codex`；示例 artifact `.gateway_runtime/project-scope-cli-smoke-20260525-035342/summary.json`，pass=true
Mimo context                                        1048576 tokens（1M）
```

---

## 6. 客户端接入

### 6.1 Claude Code

```bash
claude_mnative() {
    export ANTHROPIC_BASE_URL="http://127.0.0.1:8885/anthropic"
    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
    export ANTHROPIC_AUTH_TOKEN="your-gateway-api-key"
    export ANTHROPIC_API_KEY=""
    export ANTHROPIC_DEFAULT_OPUS_MODEL="mimo-v2.5-pro"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="mimo-v2.5-pro"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="mimo-v2.5-pro"
    export ANTHROPIC_MODEL="mimo-v2.5-pro"
    export ANTHROPIC_SMALL_FAST_MODEL="mimo-v2.5-pro"
    export ENABLE_LSP_TOOL="1"
    local claude_bin="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
    if [ -z "$claude_bin" ]; then echo "Claude binary not found; set CLAUDE_BIN" >&2; return 127; fi
    "$claude_bin" --dangerously-skip-permissions "$@"
}

# 非交互验活；期望返回 OK 或等价短答。
claude_mnative -p "Reply with OK only."
```

`ANTHROPIC_BASE_URL` 推荐带 `/anthropic` 前缀。Claude Code 会请求
`/anthropic/v1/messages` / `/anthropic/v1/messages/count_tokens`，Gateway 在
HTTP 入口规范化为内部 `/v1/messages` / `/v1/messages/count_tokens`。下游鉴权
同时接受 `Authorization: Bearer <key>` 和 Anthropic SDK 常见的 `x-api-key: <key>`。
不要把 Claude Code 直接指向真实测试上游：该上游没有 `/anthropic/v1/messages` 别名，也没有本地工具执行端点。
即使该上游 `/v1/messages` forced probe 可返回 `tool_use`，Claude Code 仍需要 Gateway 的 `/anthropic` 兼容别名、SSE 规范化和本地工具 runtime。

### 6.2 OpenCode

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8885/v1
export OPENAI_API_KEY=your-gateway-api-key
opencode
```

### 6.3 Codex CLI

```bash
export OPENAI_API_KEY=your-gateway-api-key
codex
```

`~/.codex/config.toml` 片段：

```toml
model_provider = "gateway"
model = "mimo-v2.5-pro"
model_reasoning_effort = "xhigh"
model_context_window = 1048576
model_max_output_tokens = 131072

[model_providers.gateway]
name = "gateway"
base_url = "http://127.0.0.1:8885/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
```

### 6.4 Python OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8885/v1",
    api_key="your-gateway-api-key"
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}]
)
```

---

## 7. 生产环境部署

### 7.1 安全配置

```bash
# 1. 修改管理员密码（推荐用环境变量，不提交本地配置）
export GATEWAY_ADMIN_PASSWORD='replace-with-strong-password'

# 2. 强制下游 API Key 鉴权
export GATEWAY_DOWNSTREAM_KEY='replace-with-client-key'

# 3. 最小权限工具策略；仅可信本地 coding-agent workspace 才开启
export GATEWAY_ALLOW_WRITE_TOOLS=0
export GATEWAY_ALLOW_SHELL_TOOLS=0
```

### 7.2 Linux systemd 服务

创建 `/etc/systemd/system/gateway.service`：

```ini
[Unit]
Description=AI Tool FunctionCall Gateway
After=network.target

[Service]
Type=simple
User=gateway
Group=gateway
WorkingDirectory=/opt/ai-tool-functioncall
Environment=GATEWAY_PORT=8885
Environment=GATEWAY_HOST=0.0.0.0
ExecStart=/usr/bin/python3 src/toolcall_gateway.py --host 0.0.0.0 --port 8885
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable gateway
sudo systemctl start gateway
sudo systemctl status gateway
```

### 7.3 Docker 部署

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY . .

RUN cp gateway.config.json .gateway_service.json

EXPOSE 8885

CMD ["python3", "src/toolcall_gateway.py", "--host", "0.0.0.0", "--port", "8885"]
```

```bash
docker build -t gateway .
docker run -d -p 8885:8885 -v ./config:/app/.gateway_service.json gateway
```

### 7.4 Nginx 反向代理

```nginx
server {
    listen 443 ssl;
    server_name gateway.example.com;

    ssl_certificate /etc/ssl/certs/gateway.crt;
    ssl_certificate_key /etc/ssl/private/gateway.key;

    location / {
        proxy_pass http://127.0.0.1:8885;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket 支持（SSE 流式）
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
    }
}
```

---

## 8. 常见问题

### Q: 启动失败，端口被占用

```bash
# 查看占用端口的进程
lsof -i :8885

# 强制停止
./scripts/mimo_gateway.sh stop

# 或使用其他端口
GATEWAY_PORT=9000 ./scripts/mimo_gateway.sh start
```

### Q: 无法连接上游 API

检查配置：

```bash
# 测试上游连接
curl $UPSTREAM_BASE_URL/v1/models -H "Authorization: Bearer $UPSTREAM_API_KEY"
```

### Q: 工具调用不生效

确保配置正确：

```json
{
  "upstream": {
    "tools_enabled": "adapter"
  },
  "gateway": {
    "tool_mode": "orchestrate"
  }
}
```

### Q: 如何查看日志

```bash
# 实时日志
tail -f .gateway_runtime/gateway-8885.log

# SQLite 日志查询
sqlite3 gateway_log.sqlite3 "SELECT * FROM request_logs ORDER BY id DESC LIMIT 10;"
```

### Q: Linux 上 computer_use 工具不工作

```bash
# 安装可选依赖
pip install pyautogui pillow

# 对于无头服务器，需要虚拟显示
apt install xvfb
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 &
```

---

## 附录

### A. 项目结构

```
ai_tool_functioncall/
├── src/
│   ├── toolcall_gateway.py      # 入口文件
│   ├── gateway_app.py           # 入口 + 兼容重导出
│   ├── gateway_builtin_tools.py # 内置工具实现
│   ├── gateway_streaming.py     # SSE 流式处理
│   ├── gateway_tool_runtime.py  # 工具运行时 / 编排
│   └── gateway_computer_use.py  # 电脑控制工具
├── scripts/
│   └── mimo_gateway.sh          # 启动脚本
├── tests/                       # 测试文件
├── docs/                        # 文档
├── gateway.config.json          # 配置模板
└── .gateway_service.json        # 本地配置（不入 git）
```

### B. API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/healthz` | GET | 健康检查 |
| `/ui` | GET | Admin UI |
| `/client-config` | GET | 下游客户端配置片段 |
| `/client-config.json` | GET | 下游客户端配置片段（JSON 格式） |
| `/admin/upstream-models.json` | GET/POST | Admin 鉴权后拉取上游 `/v1/models`；GET 只用已保存 active profile，POST 可用表单临时配置 |
| `/v1/models` | GET | 模型列表 |
| `/v1/chat/completions` | POST | OpenAI Chat 接口 |
| `/v1/responses` | POST | OpenAI Responses 接口 |
| `/v1/messages` | POST | Anthropic Messages 接口 |
| `/anthropic/v1/messages` | POST | Claude Code / Anthropic SDK base URL 兼容别名，内部映射到 `/v1/messages` |
| `/v1/messages/count_tokens` | POST | Anthropic token count 兼容 |
| `/anthropic/v1/messages/count_tokens` | POST | Anthropic token count 兼容别名 |
| `/v1/chat/completions/count_tokens` | POST | Chat token count 兼容 |
| `/v1/tools/call` | POST | 直接工具调用 |
| `/v1/functions/call` | POST | 直接工具调用兼容路径 |
| `/tools/call` | POST | 直接工具调用兼容路径（无 `/v1` 前缀） |

### C. 支持的工具

| 工具 | 说明 | 权限 |
|------|------|------|
| Read | 读取文件 | 默认开启 |
| Write | 写入文件 | 需 allow_write_tools |
| Edit | 编辑文件 | 需 allow_write_tools |
| Shell | 执行命令 | 需 allow_shell_tools |
| Grep | 搜索内容 | 默认开启 |
| Glob | 搜索文件 | 默认开启 |
| computer_use | 截图控制 | 需可选依赖 |

---

**最后更新**: 2026-05-24
