# API Tools / Function Call Gateway

本工程是一个**中游 Gateway**：位于三方上游 LLM API 与下游 coding-agent / SDK 客户端之间，负责协议转换、工具能力补齐、配置管理和长上下文治理。

```text
上游：三方 API
  - Chat API：完全不支持 tools
  - Sub API：部分支持 tools / function call
  - Full API：完整支持 tools / function call / tool use

中游：本项目 Gateway
  - 协议转换
  - 工具桥接 / 本地真实执行
  - 上游能力配置
  - 上下文压缩、记忆、fan-out

下游：Codex / Claude Code / DeepSeek-TUI / OpenCode / OpenAI SDK / Anthropic SDK
```

原则：

```text
可以 adapter，但不能 fake。
可以 fallback，但 fallback 必须连接真实工具 runtime 或真实 native-tools provider。
```

---

## 这套工程要解决什么

1. **协议转换**
   下游可以使用 OpenAI Chat、OpenAI Responses、Anthropic Messages 三类接口；上游也可以是这三类格式之一。Gateway 在中间做请求/响应互转。

2. **工具能力补齐**
   如果上游完全不支持 tools，或只部分支持 tools，Gateway 可以把工具定义以文本协议方式提供给上游模型，解析上游返回的文本工具调用，本地执行真实工具，再把真实结果回填继续生成。对下游来说，经过 Gateway 的请求仍然具备可编程、可工具调用的能力。

3. **配置与能力声明**
   Gateway 提供 Web UI 和配置文件，用来管理上游 profile、下游 key、上游是否支持 tools、function calls、parallel tool calls、vision/识图、streaming、JSON schema、上下文窗口、工具权限、MCP、HTTP Actions 等。Admin UI 可通过上游 `/v1/models` 自动拉取模型列表。

4. **长上下文 / 类无限上下文**
   Gateway 支持 token 估算、压缩、SQLite 记忆召回、超长输入 fan-out 分片与综合，让下游获得类似“无限上下文”的使用效果。

---

## 当前能力概览

| 能力 | 当前状态 | 主要模块 |
|---|---|---|
| OpenAI Chat / Responses / Anthropic Messages 协议互转 | 已实现并有测试 | `src/gateway_protocol.py` |
| HTTP API 入口、认证、ACL、错误映射 | 已实现并有测试 | `src/gateway_http_handler.py`, `src/gateway_errors.py` |
| 上游代理与路径映射 | 已实现 | `src/gateway_proxy.py`, `src/gateway_config.py` |
| 工具调用编排、多轮执行、文本 fallback | 已实现并有测试 | `src/gateway_tool_runtime.py` |
| 内置 coding-agent 工具 | 已实现，当前 67 个唯一工具名 | `src/gateway_builtin_tools.py` |
| MCP / HTTP Action 扩展 | 已实现基础能力 | `src/gateway_mcp.py`, `src/gateway_http_actions.py` |
| 流式 SSE 编排 | 已实现并有测试 | `src/gateway_streaming.py` |
| 上下文压缩、SQLite 记忆、fan-out | 已实现并有测试 | `src/gateway_context.py` |
| Admin UI / client config snippets / 上游模型自动获取 | 已实现并有测试 | `src/gateway_admin.py`, `src/gateway_http_handler.py` |

当前回归测试：

```bash
python3 -m pytest -q
# 167 passed

GATEWAY_VERIFY_MODEL_REQUESTS=0 GATEWAY_VERIFY_DIRECT_REQUESTS=24 GATEWAY_VERIFY_WORKERS=8 ./scripts/mimo_gateway.sh verify
# 167 unittest tests + tool acceptance + security/auth guardrails + concurrency smoke OK
```

本轮真实 8885 稳定性复验（2026-05-24）：

```text
./scripts/mimo_gateway.sh start                OK
GET  /healthz                                  OK，builtin_tool_count=67
GET  /ui                                       OK，包含 Gateway Control Center / Capability Matrix / Fetch Models / claude_mnative()
GET  /admin/upstream-models.json               OK，从真实上游返回 mimo-v2.5-pro 等模型
POST /v1/chat/completions                      OK
POST /anthropic/v1/messages                    OK，返回严格 Anthropic message shape
POST /anthropic/v1/messages + tools(calc/expr) OK，无 too-long / malformed response
POST /v1/tools/call calc expr=2+2              OK，返回 4
POST /v1/functions/call calc expr=6*7          OK，返回 42
Claude Code local-file smoke                  OK，stdout=2+2=4，无 too-long / malformed / empty response
Codex /v1/responses tool chain calc/expr       OK，回归覆盖到工具结果回填
Codex /v1/responses strict shape               OK，返回 object=response/id/status/usage
Claude /v1/messages tool chain calc/expr       OK，回归覆盖到 tool_use 结果回填
artifact: .gateway_runtime/final-smoke-20260524-074454-goal-audit/summary.json
claude-local-file-artifact: .gateway_runtime/claude-local-file-probe-20260524-074546-goal-audit.summary.json
```

---

## 快速开始

```bash
# 1. 创建本地配置（已有 .gateway_service.json 时可跳过）
cp gateway.config.json .gateway_service.json
vi .gateway_service.json

# 至少确认这些值：
# - upstream.base_url / upstream.api_key / upstream.model
# - gateway.client_snippet_api_key 或环境变量 GATEWAY_DOWNSTREAM_KEY
# - admin.password 或环境变量 GATEWAY_ADMIN_PASSWORD
# 注意：README/docs 中的 test-gateway-key / upstream.example.local 都是占位示例；真实账号只放 .gateway_service.json 或环境变量，不提交。

# 2. 后台启动（默认端口 8885）
./scripts/mimo_gateway.sh start

# 3. 验证是否运行
curl http://127.0.0.1:8885/healthz
./scripts/mimo_gateway.sh status
```

默认入口：

```text
API:       http://127.0.0.1:8885/v1/...
Admin UI:  http://127.0.0.1:8885/ui
管理员:    admin / admin（仅开发默认值；首次配置请改强密码）
```

### 启动 / 停止 / 排查命令

推荐统一走 `scripts/mimo_gateway.sh`，它会读取 `.gateway_service.json`，按需创建本地配置，并把日志写到 `.gateway_runtime/gateway-8885.log`。

```bash
# 后台启动：优先使用 screen；没有 screen 时退回 nohup
./scripts/mimo_gateway.sh start

# 前台运行：适合本地调试，Ctrl-C 退出
./scripts/mimo_gateway.sh foreground

# 查看当前监听、健康状态、日志路径
./scripts/mimo_gateway.sh status

# 查看日志
./scripts/mimo_gateway.sh logs

# 停止 / 重启
./scripts/mimo_gateway.sh stop
./scripts/mimo_gateway.sh restart

# macOS launchd 后台运行
GATEWAY_START_METHOD=launchd ./scripts/mimo_gateway.sh start

# 强制使用 nohup
GATEWAY_START_METHOD=nohup ./scripts/mimo_gateway.sh start
```

常用环境变量：

```bash
# 改端口 / 监听地址
GATEWAY_PORT=9000 GATEWAY_HOST=127.0.0.1 ./scripts/mimo_gateway.sh start

# 使用临时配置做 smoke，不影响默认 .gateway_service.json
TMPDIR=$(mktemp -d)
GATEWAY_CONFIG_PATH="$TMPDIR/config.json" \
GATEWAY_SQLITE_LOG_PATH="$TMPDIR/gateway_log.sqlite3" \
GATEWAY_DOWNSTREAM_KEY=smoke-key \
GATEWAY_ADMIN_PASSWORD=admin \
UPSTREAM_BASE_URL="http://127.0.0.1:9001" \
UPSTREAM_MODEL="smoke-model" \
python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8899
```

验活命令：

```bash
curl -fsS http://127.0.0.1:8885/healthz
curl -fsS -u admin:admin http://127.0.0.1:8885/ui >/dev/null
curl -fsS -u admin:admin http://127.0.0.1:8885/client-config.json >/dev/null

# 直接工具调用只依赖 Gateway 本地工具 runtime，不依赖上游模型：
curl -fsS http://127.0.0.1:8885/v1/tools/call \
  -H 'authorization: Bearer <your-gateway-key>' \
  -H 'content-type: application/json' \
  -d '{"tool":"calculator","arguments":{"expression":"6*7"}}'
```

> `/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 需要 `UPSTREAM_BASE_URL` / API key 指向可访问的真实上游；未配置上游时，Gateway 自身可以启动，但这些上游转发接口会返回上游配置/连接错误。

---

## 下游接入

### Claude Code

推荐用 Anthropic-compatible 前缀 `/anthropic`，这样 Claude Code 会请求
`http://127.0.0.1:8885/anthropic/v1/messages`，Gateway 会规范化到内部
`/v1/messages` 再按当前上游 profile 转发。

```bash
claude_mnative() {
    export ANTHROPIC_BASE_URL="http://127.0.0.1:8885/anthropic"
    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
    export ANTHROPIC_AUTH_TOKEN="your-gateway-key"
    export ANTHROPIC_API_KEY=""
    export ANTHROPIC_DEFAULT_OPUS_MODEL="mimo-v2.5-pro"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="mimo-v2.5-pro"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="mimo-v2.5-pro"
    export ANTHROPIC_MODEL="mimo-v2.5-pro"
    export ANTHROPIC_SMALL_FAST_MODEL="mimo-v2.5-pro"
    export ENABLE_LSP_TOOL="1"
    /opt/homebrew/bin/claude --dangerously-skip-permissions "$@"
}

# 非交互验活
claude_mnative -p "Reply with OK only."
```

兼容说明：Gateway 同时接受 `Authorization: Bearer <key>` 和 `x-api-key: <key>`；
`/anthropic/v1/messages` 与 `/anthropic/v1/messages/count_tokens` 会映射到对应的
`/v1/messages` 与 `/v1/messages/count_tokens`。

### Codex / OpenCode / DeepSeek-TUI / OpenAI SDK

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8885/v1"
export OPENAI_API_KEY="your-gateway-key"
```

Python SDK 示例：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8885/v1", api_key="your-gateway-key")
```

---

## API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/healthz` | GET | 健康检查 |
| `/ui` | GET | Admin UI |
| `/client-config` | GET | 下游客户端配置片段 |
| `/admin/upstream-models.json` | GET/POST | Admin 鉴权后拉取上游 `/v1/models`；GET 只用已保存 active profile，POST 可用表单临时配置 |
| `/v1/models` | GET | 模型列表 |
| `/v1/chat/completions` | POST | OpenAI Chat |
| `/v1/responses` | POST | OpenAI Responses |
| `/v1/messages` | POST | Anthropic Messages |
| `/anthropic/v1/messages` | POST | Claude Code / Anthropic SDK base URL 兼容别名，内部映射到 `/v1/messages` |
| `/v1/messages/count_tokens` | POST | Anthropic token count 兼容 |
| `/anthropic/v1/messages/count_tokens` | POST | Anthropic token count 兼容别名 |
| `/v1/chat/completions/count_tokens` | POST | Chat token count 兼容 |
| `/v1/tools/call` | POST | 直接工具调用 |
| `/v1/functions/call` | POST | 直接工具调用兼容路径 |
| `/tools/call` | POST | 直接工具调用兼容路径（无 `/v1` 前缀） |

---

## 当前代码结构

```text
src/
├── toolcall_gateway.py       # 兼容入口；导入时映射到 gateway_app
├── gateway_app.py            # 主入口 + 旧单体兼容重导出层
├── gateway_config.py         # 配置、上游 profile、路径映射、能力开关
├── gateway_errors.py         # 统一错误类型和 error payload
├── gateway_http_handler.py   # HTTP 路由、下游 key 认证、ACL、错误映射
├── gateway_admin.py          # Admin UI / client config 页面渲染
├── gateway_protocol.py       # 三协议请求/响应/工具格式转换
├── gateway_proxy.py          # 上游 HTTP 客户端
├── gateway_tool_runtime.py   # 工具解析、规范化、多轮编排、直接调用
├── gateway_builtin_tools.py  # 内置工具真实实现
├── gateway_streaming.py      # SSE 流式编排与事件转换
├── gateway_context.py        # token 估算、压缩、记忆、fan-out
├── gateway_mcp.py            # MCP client / tools/list / tools/call
├── gateway_http_actions.py   # HTTP Action executor
├── gateway_logging.py        # SQLite / JSONL 日志、统计、失败记录
└── gateway_computer_use.py   # GUI / computer-use 辅助工具
```

---

## 文档索引

| 文档 | 用途 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 上游/中游/下游定位、模块边界、请求流程 |
| [`docs/CURRENT_AUDIT.md`](docs/CURRENT_AUDIT.md) | 当前审计结论、风险点核验、已修复项 |
| [`docs/RUNNING_AND_TESTING.md`](docs/RUNNING_AND_TESTING.md) | 部署、配置、启动、测试 |
| [`docs/gateway-admin-ui-config.md`](docs/gateway-admin-ui-config.md) | Admin UI、下游 key、能力配置 |
| [`docs/gateway-infinite-context-memory.md`](docs/gateway-infinite-context-memory.md) | 长上下文、SQLite 记忆、fan-out |
| [`docs/full-gateway-tool-runtime-marketplace.md`](docs/full-gateway-tool-runtime-marketplace.md) | 完整 Tool Runtime / Marketplace 方案 |
| [`docs/coding-agent-builtin-tools-implementation.md`](docs/coding-agent-builtin-tools-implementation.md) | 内置 coding-agent 工具实现 |

---

## 重要边界

- Gateway 不把假的 tool result 伪装成真实成功。
- 文本 fallback 只是一种弱上游兼容方式；工具仍由 Gateway/MCP/HTTP Action/真实 executor 执行。
- 写文件、Shell、GUI、网络类工具要通过配置显式授权；公开模板和 Docker 默认关闭写入/Shell。
- `admin.password` 模板字段会在加载/保存时转换为 `password_hash`，避免明文密码被回写。
- `gateway.client_snippet_api_key` 会自动同步成可认证的 downstream key，避免复制出的客户端配置不可用。
- 上游 `tools_enabled=auto` 会结合 `upstream.capabilities.supports_tools` / `supports_function_calls` 判断是否发送原生 tools；若能力关闭，会自动走本地真实工具文本适配，`native_only` 则会 fail-fast。
- `gateway.text_tool_adapter_compact_token_limit` 是弱上游文本工具适配前的压缩阈值上限（默认 48000）；实际阈值动态计算为 `max(8000, min(upstream.max_input_tokens * 0.45, 此值))`，设为 0 可关闭。
- 已存在配置文件如果 JSON 损坏或根节点不是对象，会 fail closed 返回结构化 500；不会回退到默认 `admin/admin` 或无下游鉴权。
- 请求/响应日志和 Admin 配置展示会递归遮盖常见敏感字段（token、secret、password、cookie、API key、key hash 等），避免运维面泄漏凭据。
- Admin 写操作会拒绝跨源浏览器 Origin/Referer 请求；无来源头的 CLI/脚本请求仍保持兼容。
- HTTP POST 请求体有读取前上限，默认 64MB；可通过 `gateway.max_request_body_bytes` / `GATEWAY_MAX_REQUEST_BODY_BYTES` 调整，超限返回 413。
- 请求/响应日志和 tool failure 内容会先遮盖敏感字段，再按 `gateway.max_log_payload_chars` / `GATEWAY_MAX_LOG_PAYLOAD_CHARS` 截断，避免 SQLite/JSONL 膨胀。
- `gateway_app.py` 当前保留旧单体兼容导出层，新增实现应优先放入对应 `gateway_*` 模块。
