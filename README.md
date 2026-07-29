# API Tools / Function Call Gateway

本工程是一个**中游 Gateway**：位于三方上游 LLM API 与下游 coding-agent / SDK 客户端之间，负责协议转换、工具能力补齐、配置管理和长上下文治理。

``` text
上游：三方 API
  - Chat API：完全不支持 tools
  - Sub API：部分支持 tools / function call
  - Full API：完整支持 tools / function call / tool use

中游：本项目 Gateway
  - 协议转换
  - 工具桥接 / 归属分流（Gateway-owned 真执行，用户侧下发客户端执行）
  - 上游能力配置
  - 上下文压缩、记忆、fan-out

下游：Codex / Claude Code / DeepSeek-TUI / OpenCode / OpenAI SDK / Anthropic SDK
```

原则：

``` text
可以 adapter，但不能 fake。
可以 fallback，但 fallback 必须连接真实工具 runtime、真实 native-tools provider，或返回下游原生工具请求让客户端执行。
```

---

## 这套工程要解决什么

1. **协议转换**
   下游可以使用 OpenAI Chat、OpenAI Responses、Anthropic Messages 三类接口；上游也可以是这三类格式之一。Gateway 在中间做请求/响应互转。

2. **工具能力补齐**
   如果上游完全不支持 tools，或只部分支持 tools，Gateway 可以把工具定义以文本协议方式提供给上游模型，解析上游返回的文本工具调用，再按工具归属处理：HTTP Action/MCP/网络/纯函数/记忆等 Gateway-owned 工具由 Gateway 真执行并回填；Read/LS/Bash/Skill/GUI/local agent 等用户侧工具返回下游原生工具请求，由 Claude Code/Codex 在用户机器执行并把结果回传。

3. **配置与能力声明**
   Gateway 提供受 Basic Auth 保护的运维控制台，以及独立的 9-Tab `/ui/config` Config Center。运维面可管理上游 profile、下游 key、能力矩阵、MCP、HTTP Actions 和 **Skills**；Config Center 通过带 revision 的原子更新管理实际运行时字段。Admin UI 可通过上游 `/v1/models` 自动拉取模型列表。

4. **长上下文 / 类无限上下文**
   Gateway 支持 token 估算、压缩、SQLite 记忆召回、超长输入 fan-out 分片与综合，让下游获得类似“无限上下文”的使用效果。

---

## 当前能力概览

| 能力 | 当前状态 | 主要模块 |
|---|---|---|
| OpenAI Chat / Responses / Anthropic Messages 协议互转 | ✅ 已实现 | `src/gateway_protocol.py` |
| HTTP API 入口、认证、ACL、错误映射 | ✅ 已实现 | `src/gateway_http_handler.py` |
| 上游代理、连接复用、自动重试 | ✅ 已实现 | `src/gateway_proxy.py` |
| 工具调用编排、多轮执行、文本 fallback | ✅ 已实现 | `src/gateway_tool_runtime.py` |
| 内置 coding-agent 工具（67 个唯一工具；含别名 178 个注册项） | ✅ 已实现 | `src/gateway_builtin_tools.py` |
| MCP / HTTP Action 扩展 | ✅ 已实现 | `src/gateway_mcp.py`, `src/gateway_http_actions.py` |
| 流式 SSE 编排 + 流式缓存 | ✅ 已实现 | `src/gateway_streaming.py` |
| 上下文压缩、SQLite 记忆、fan-out | ✅ 已实现 | `src/gateway_context.py` |
| 语义缓存（精确 + 相似匹配） | ✅ 已实现 | `src/gateway_cache.py` |
| 请求前 Intelligence 分析（规则 + 可插拔 LLM provider） | ✅ 已接入非流式与流式编排；响应后质量评分/二次反思尚未接线 | `src/gateway_intelligence.py`, `src/gateway_llm.py` |
| HTTP 请求/工具统计与辅助 Q&A 统计库 | ✅ 主链由 `gateway_logging.py` 采集；`gateway_stats.py` 为辅助数据源 | `src/gateway_logging.py`, `src/gateway_stats.py` |
| Web 配置 UI（9 Tab + 实时状态卡） | ✅ 已实现；自动读取 stats/cache/upstream/intelligence 状态 | `src/gateway_web_config.py` |
| Assistants / Threads | ✅ Gateway-owned SQLite 生命周期、messages、runs、steps、tool-output resume、租户隔离 | `src/gateway_assistants.py` |
| Web2API（网页转结构化 API） | ✅ 已接 `/v1/web2api`、`/api/web2api`、`/anthropic/v1/web2api` | `src/gateway_web2api.py` |
| 单上游连接复用、重试、限界 | ✅ 已接入 | `src/gateway_proxy.py` |
| 多上游负载均衡 / 故障转移 | ✅ 非流式 canonical path 支持跨 profile；SSE 不跨 profile | `src/gateway_upstream_pool.py`, `src/gateway_proxy.py` |
| Claude Code 兼容层 | ✅ 已实现 | `src/gateway_claude_compat.py` |
| Admin UI / Config API / cache、stats、provider 状态 | ✅ 已实现 | `src/gateway_admin.py`, `src/gateway_admin_api.py` |

运行时能力以 `GET /capabilities` 为准。模块文件存在或单元测试通过，不代表该模块已经接入生产 HTTP 请求路径。上述工具数字是 2026-07-28 对当前 registry 的运行时快照：67 个唯一 canonical 工具，178 个含别名 key。

当前回归测试（以实际门禁输出为准）：

```bash
# 快速全量回归
python3 -m pytest -q

# smoke-level Agent Planner / protocol / public-surface acceptance
./scripts/agent_planner_acceptance.sh

# Agent Planner full gate（smoke、focused tests、clean-env 全量 pytest）
./scripts/agent_planner_acceptance.sh --full

# 完整 CI 门禁：静态检查、依赖审计、pytest、配置、Compose、Docker
./scripts/ci_gate.sh
```

最近一次 Agent Planner 完整验收（2026-07-28）已通过：全部 integration smoke、focused gate `92 passed`、clean-env 全量 pytest `1495 passed, 2 skipped`，最终输出 `Agent Planner acceptance gate: PASS`。脚本会在全量阶段移除 `GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN` 覆盖，不再污染仓库默认值测试；两个 skip 是未设置 `TEST_UPSTREAM_URL` 的外部上游用例。同日完整 CI 也通过：compile/config、Ruff、17 个模块的 Mypy、Bandit、`pip check`、`pip-audit`（无已知漏洞）、pytest、Git/secret guard、两套 Compose 渲染和 Docker 镜像构建/删除均成功。GitHub CI 会显式安装 Chromium，并要求 Config Center 的 Playwright 浏览器回归真实执行；缺少 Playwright 或 Chromium 会使门禁失败。生产/公网部署前仍必须配置非默认 Admin 凭证，并以部署环境自己的门禁输出为准。

本轮真实测试上游 / Mimo 兼容结论（2026-05-25，地址只保存在本地 ignored 配置或环境变量中）：

```text
GET  /v1/models                         OK，返回 mimo-v2.5-pro 等模型
POST /v1/chat/completions               OK
POST /v1/responses                      OK
POST /v1/messages                       OK
POST /anthropic/v1/messages             上游直连 404（必须通过本 Gateway 的 /anthropic 别名）
POST /v1/tools/call / /v1/functions/call 上游直连 404（必须由 Gateway adapter 或下游客户端补齐）
POST /v1/messages + forced tool_choice   OK，可返回 Anthropic tool_use（例如 echo_probe）
POST /v1/responses + forced tool probe    未返回 function_call，仅 reasoning/message

正确接入方式：
Claude Code -> http://127.0.0.1:8885/anthropic -> Gateway adapter/orchestrate -> Mimo text reasoning
Codex       -> http://127.0.0.1:8885/v1        -> Gateway adapter/orchestrate -> Mimo text reasoning
工具执行     -> Gateway-owned 工具在 Gateway 真执行；Read/LS/Bash/Skill/GUI 等用户侧工具下发给客户端执行。
项目目录     -> 请求体显式 root 或下游客户端项目目录；不是 Gateway 服务启动目录。
Skills/插件  -> Admin 可展示 Gateway 可见 skills；对话中的项目 Skill 默认下发给客户端执行。

Mimo 上下文按 1M 配置：upstream.max_input_tokens=1048576，gateway/client_context_window=1048576。
已覆盖：Anthropic SSE tool_use.input 规范化为 input_json_delta；streaming adapter 对 Read/Bash/Skill 返回下游工具请求；direct `/v1/tools/call` / `/v1/functions/call`；Claude Code / Codex 项目目录识别；项目级 `.traces` 不串服务目录；项目内 plugin skills；Memory/RecallMemory 项目根隔离；streaming passthrough 也会剥离 Gateway 内部 workspace 路由字段。
可复跑的项目级 smoke：`tests/integration/project_scope_cli_smoke.py`；`./scripts/mimo_gateway.sh verify` 已包含它，设置 `GATEWAY_VERIFY_REQUIRE_CLI=1` 时会把 Claude Code CLI 与 Codex CLI 也作为必过项。示例 artifact: `.gateway_runtime/project-scope-cli-smoke-20260525-035342/summary.json`，`pass=true`。
```

`verify` 只在真实本地工具验收阶段临时开启服务端用户工具/localhost 夹具权限，单元测试与最终 Claude/Codex Skill 归属检查会恢复生产安全边界；自定义 Admin 凭据可通过 `GATEWAY_VERIFY_ADMIN_USERNAME` / `GATEWAY_VERIFY_ADMIN_PASSWORD` 提供，不会进入 argv 或 launchd plist。

---

## 快速开始

```bash
# 1. 创建本地配置（已有 .gateway_service.json 时可跳过）
cp gateway.config.json .gateway_service.json
vi .gateway_service.json

# 至少确认这些值：
# - upstream.base_url=<你的真实测试上游地址> / upstream.model=mimo-v2.5-pro
# - upstream.tools_enabled=adapter，capabilities.supports_tools=false，supports_function_calls=false
#   说明：Mimo /v1/messages forced probe 可返回 tool_use，但 /anthropic 别名、direct tool endpoint、
#   Codex /v1/responses function_call 未证实；为了 Claude Code + Codex 一致稳定，默认仍走 Gateway adapter。
# - upstream.max_input_tokens=1048576，gateway.client_context_window=1048576
# - gateway.execute_user_side_tools_in_gateway=false（默认）：Read/LS/Bash/Skill/GUI 等用户机器工具下发给 Claude Code/Codex 执行
# - gateway.client_snippet_api_key 或环境变量 GATEWAY_DOWNSTREAM_KEY
# - admin.password 或环境变量 GATEWAY_ADMIN_PASSWORD
# - 下游项目目录优先来自请求体 workspace_root/gateway_workspace、Claude Code 的
#   Primary working directory/Worktree、Codex Responses 的 <environment_context><cwd>
# 注意：真实账号/API key 只放 .gateway_service.json 或环境变量，不提交。

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

# 可选：指定 Admin 管理的服务级 Skill catalog
GATEWAY_ADMIN_SKILLS_ROOT=/srv/ai-gateway/skills ./scripts/mimo_gateway.sh start

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

如果不想触达真实测试上游，也可以先起一个本地弱上游 mock：

```bash
python3 scripts/mock_openai_upstream.py --port 9001 --model mimo-v2.5-pro
UPSTREAM_BASE_URL=http://127.0.0.1:9001 ./scripts/mimo_gateway.sh start
```

验活命令：

```bash
curl -fsS http://127.0.0.1:8885/healthz
curl -fsS -u admin:admin http://127.0.0.1:8885/ui >/dev/null
curl -fsS -u admin:admin http://127.0.0.1:8885/client-config.json >/dev/null

# Gateway-owned 直接工具调用不依赖上游模型：
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
`/v1/messages` 再按当前上游 profile 转发。不要把 Claude Code 直接指向
真实测试上游：该上游没有 `/anthropic/v1/messages` 别名，也没有本地工具执行端点；
即使 `/v1/messages` 强制探针可返回 `tool_use`，Claude Code 仍需要 Gateway 的别名、SSE 规范化和本地 runtime。

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
    local claude_bin="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
    if [ -z "$claude_bin" ]; then echo "Claude binary not found; set CLAUDE_BIN" >&2; return 127; fi
    "$claude_bin" --dangerously-skip-permissions "$@"
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
# Codex 建议 wire_api=responses；base_url 指本 Gateway /v1，不要直连 Mimo 上游。
```

Codex `config.toml` 片段：

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
| `/v1/web2api` / `/api/web2api` / `/anthropic/v1/web2api` | POST | 受下游认证、ACL、限流、准入和 SSRF 边界保护的网页结构化提取 |
| `/v1/assistants` / `/v1/assistants/{id}` | GET/POST/DELETE | Assistant 列表、创建、读取、修改、删除 |
| `/v1/threads` / `/v1/threads/{id}` | POST/GET/DELETE | Thread 创建、读取、修改、删除 |
| `/v1/threads/{thread_id}/messages` | GET/POST | Message 列表和创建；资源路径另支持读取、修改、删除 |
| `/v1/threads/{thread_id}/runs` | GET/POST | Run 列表和同步创建；另支持读取、修改、取消、提交 tool outputs 和 steps |
| `/ui/config` | GET | 9-Tab Config Center（Basic Auth） |
| `/api/config` / `/api/config/schema` | GET | 脱敏配置、revision 和可编辑 schema（Basic Auth） |
| `/api/config` / `/api/config/update` | POST | revision-aware 原子配置更新（Basic Auth + same-origin） |
| `/api/stats/dashboard` / `/api/cache/stats` | GET | 统计和缓存状态（Basic Auth） |
| `/api/cache/clear` | POST | 清除内存及持久语义/工具缓存（Basic Auth + same-origin） |
| `/api/upstreams/status` / `/api/intelligence/status` | GET | 脱敏上游池和 LLM provider 状态（Basic Auth） |

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
├── gateway_builtin_tools.py  # 内置工具真实实现（67 unique / 178 including aliases）
├── gateway_streaming.py      # SSE 流式编排 + 流式缓存
├── gateway_context.py        # token 估算、压缩、记忆、fan-out
├── gateway_cache.py          # 语义缓存 (精确/相似匹配)
├── gateway_intelligence.py   # 请求前问题分析/prompt enhancement；另含未接响应链的质量/反思 helper
├── gateway_llm.py            # 可插拔 LLM intelligence provider
├── gateway_stats.py          # 辅助 Q&A 统计库与 dashboard 数据源
├── gateway_web_config.py     # Web 配置 UI (9 Tab)
├── gateway_admin_api.py      # revision-aware 配置、统计和缓存管理 API
├── gateway_web2api.py        # Web → 结构化 API
├── gateway_upstream_pool.py  # 多上游选择、熔断、恢复和状态快照
├── gateway_assistants.py     # Assistants / Threads SQLite 生命周期
├── gateway_claude_compat.py  # Claude Code 兼容层
├── gateway_mcp.py            # MCP client / tools/list / tools/call
├── gateway_http_actions.py   # HTTP Action executor
├── gateway_logging.py        # 主 HTTP 请求/工具统计、SQLite / JSONL 日志、失败记录
└── gateway_computer_use.py   # GUI / computer-use 辅助工具
```

---

## 文档索引

| 文档 | 用途 |
|---|---|
| [`docs/README.md`](docs/README.md) | **统一文档中心**：当前入口、权威级别、全部文档分类和能力覆盖矩阵 |
| [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) | 当前能力、生产调用链、边界与最新验证证据 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 上游/中游/下游定位、模块边界、请求流程 |
| [`docs/RUNNING_AND_TESTING.md`](docs/RUNNING_AND_TESTING.md) | 部署、配置、启动、测试、API 验证 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | 生产环境部署指南 |
| [`docs/guides/`](docs/guides/README.md) | 当前操作指南：弱上游 adapter、curl、Admin/Config |
| [`docs/reference/`](docs/reference/README.md) | 当前技术参考：Agent Runtime、工具、上下文、格式映射 |
| [`docs/archive/`](docs/archive/README.md) | 历史审计、旧设计、旧进度和 Workspace 修复材料 |
| [`CLAUDE.md`](CLAUDE.md) | 当前项目协作说明、所有权边界和必跑门禁 |

所有项目文档已统一整理到 `docs/`；根目录仅保留 README 与项目协作说明。带日期的审计、验收矩阵、旧设计和旧进度统一位于 `docs/archive/`，只作为历史证据，不用于覆盖当前能力声明。

---

## 安全与敏感信息

**以下文件包含真实 API 地址/密钥，已加入 `.gitignore`，不会被提交：**

| 文件 | 内容 | gitignore |
|------|------|-----------|
| `.gateway_service.json` | 上游 API 地址、密钥、下游 key | ✅ |
| `.case.txt` | 测试用 curl 命令（含真实 IP） | ✅ |
| `.gateway_runtime/` | 运行时配置缓存 | ✅ |
| `.traces/` | Claude Code 调用 trace | ✅ |
| `.env` | 环境变量（密钥） | ✅ |

**原则：真实 API 地址只放本地配置文件或环境变量，绝不写入提交代码。**

```bash
# 验证：除 intentional loopback 示例外，提交内容不应包含公网 IP URL
git grep -nE 'https?://([0-9]{1,3}\.){3}[0-9]{1,3}' -- ':!docs/archive/**' \
  | grep -v '127\.0\.0\.1'
# 应无输出；命令因 grep 未匹配返回 1 属于预期
```

---

## 重要边界

- Gateway 不把假的 tool result 伪装成真实成功。
- 真实测试上游 / Mimo 直连缺少 `/anthropic` 别名和 direct tools endpoint；`/v1/messages` forced probe 可返回 Anthropic `tool_use`，但 Codex `/v1/responses` function_call 未证实。因此 Claude Code/Codex 默认由本 Gateway 的 adapter/orchestrate 补齐协议；**用户机器工具（Read/LS/Glob/Grep/Write/Edit/Bash/Skill/GUI/local agent）默认下发给下游客户端执行**，Gateway 只执行 gateway-owned 工具（HTTP Action/MCP/网络/纯函数/记忆等）。
- 文本 fallback 只是一种弱上游兼容方式；Gateway 会把弱上游输出的 `<function=...>` 转成下游原生 `tool_use/tool_calls/function_call`，用户侧工具不在 Gateway 服务机执行。只有显式设置 `gateway.execute_user_side_tools_in_gateway=true` 时，才启用旧的本机代理式执行；legacy `delegate_tools_to_downstream=false` 本身不再授予服务端执行用户 workspace 工具的权限。
- Gateway 是中游服务，不能把服务启动目录当作用户项目目录；项目级 `.traces`、SQLite 记忆隔离、Skills/插件解析都以当前请求解析出的下游项目根为准；需要触碰用户目录/终端/GUI 的动作必须由下游 Claude Code/Codex 在用户机器完成。
- `workspace_root` / `gateway_workspace` / `projectDir` / `cwd` 等字段只作为 Gateway 内部路由信号；普通转发和 streaming passthrough 都会在上游请求前剥离，metadata JSON 字符串和 `metadata.user_id` 内嵌 JSON 里的同类字段也会清理。
- `Skill`/`list_skills`/`read_skill`/`run_skill` 涉及用户项目 skills 时默认作为下游工具请求返回；Admin UI 仍可展示 Gateway 可见的全局/额外 skills。
- 项目内插件只读取当前项目根下 `.codex/plugins`、`.claude/plugins`、`.opencode/plugins`、`plugins` 中 manifest 声明的 skills，且 skills 路径必须仍在项目根内。
- 写文件、Shell、GUI/local-agent 等用户侧工具默认下发给客户端；只有本地代理式部署显式开启 `gateway.execute_user_side_tools_in_gateway=true` 时，才会在 Gateway 服务机执行，且写入/Shell 仍需单独授权。
- `admin.password` 模板字段会在加载/保存时转换为 `password_hash`，避免明文密码被回写。
- `gateway.client_snippet_api_key` 会自动同步成可认证的 downstream key，避免复制出的客户端配置不可用。
- 默认按上游**不支持** tool calls/function calls 处理：`upstream.tools_enabled=adapter` 且 `supports_tools=false` / `supports_function_calls=false`。`auto` / `native` 只保留给显式兼容实验，不作为默认路径；Claude Code/Codex 稳定接入默认都走 Gateway Agent Planner adapter。
- `gateway.text_tool_adapter_compact_token_limit` 是弱上游文本工具适配前的压缩阈值上限（默认 48000）；实际阈值动态计算为 `max(8000, min(upstream.max_input_tokens * 0.45, 此值))`，设为 0 可关闭。
- 已存在配置文件如果 JSON 损坏或根节点不是对象，会 fail closed 返回结构化 500；不会回退到默认 `admin/admin` 或无下游鉴权。
- 请求/响应日志和 Admin 配置展示会递归遮盖常见敏感字段（token、secret、password、cookie、API key、key hash 等），避免运维面泄漏凭据。
- Admin 写操作会拒绝跨源浏览器 Origin/Referer 请求；无来源头的 CLI/脚本请求仍保持兼容。
- `/ui/config` 及所有 `/api/config|stats|cache|upstreams|intelligence` 管理接口都要求 Admin Basic Auth；配置更新使用 revision 做乐观并发控制，密码占位符 `***` 或空值不会覆盖现有 secret。
- 非流式多上游请求只对可安全重试的 429/502/503/504/transport timeout 做有界跨 profile 故障转移；400 等客户端错误不会切换上游。SSE 使用请求开始时选中的 profile，仅在首事件前对该 profile 做有界连接重试；当前不会跨 profile，首事件输出后也绝不重放。
- Assistants Runs 是 Gateway-owned 持久同步生命周期：普通回答可完成 run，function call 可进入 `requires_action` 并由 `submit_tool_outputs` 恢复；当前不宣称 Assistants Run SSE streaming。
- Intelligence 当前接线点在请求发送上游之前：分析问题并把 system/reflection prompt 注入同一次主回答。`intelligence.use_llm=true` 时使用注册 provider；默认非 strict 模式在 provider 失败时回退规则，`strict_mode=true` 则让请求明确失败。`assess_quality()` / `reflect_on_answer()` 等 helper 有库级实现和单元测试，但尚未接入响应后的自动评分、二次 LLM 反思或答案重写链。
- 主 HTTP 请求/工具统计由 `gateway_logging.py` 自动写入；`gateway_stats.py` 提供独立的请求、工具、缓存、质量和 upstream writer/query API，`/api/stats/dashboard` 会同时返回主 HTTP snapshot 与辅助 dashboard，但生产 HTTP 链当前不会自动填满所有辅助表。
- HTTP POST 请求体有读取前上限，默认 64MB；可通过 `gateway.max_request_body_bytes` / `GATEWAY_MAX_REQUEST_BODY_BYTES` 调整，超限返回 413。
- 请求/响应日志和 tool failure 内容会先遮盖敏感字段，再按 `gateway.max_log_payload_chars` / `GATEWAY_MAX_LOG_PAYLOAD_CHARS` 截断，避免 SQLite/JSONL 膨胀。
- `gateway_app.py` 当前保留旧单体兼容导出层，新增实现应优先放入对应 `gateway_*` 模块。
