# API Tools / Function Call Gateway

这个项目用于梳理并设计一套 **API Tools 支持** 能力：让 `/v1/chat/completions`、`/v1/responses`、`/v1/messages` 这类对话 API 能以可验证的方式支持 tools / function call / tool use。

当前方向已经明确：

```text
可以 adapter，但不能 fake。
可以 fallback，但 fallback 必须连接真实工具 runtime 或真实 native-tools provider。
```

也就是说：

1. 如果上游 API 原生支持 tools，就走 native passthrough。
2. 如果上游 API 不支持 tools，但用户仍要 tools，就走我们的协议适配层。
3. 协议适配层必须对接真实工具执行器、MCP、外部 function-call 服务，或另一个真实 native-tools provider。
4. 不允许把 tools 写进 prompt，再把模型文本 JSON 伪装成 `tool_calls` / `tool_use`。
5. Claude Code / Codex / OpenCode 这类客户端默认应该优先拿到真实协议级工具调用字段。

## 核心方案文档

- `docs/RUNNING_AND_TESTING.md`：运行、配置、curl 调用、MCP/HTTP Action、本地 fake upstream 闭环和自动化测试方法。
- `docs/full-gateway-tool-runtime-marketplace.md`：当前主方案。Gateway 作为完整 tool/function-call runtime，通过内置工具、MCP、OpenAPI、HTTP action、插件/脚本和外部 function service 执行工具，并把结果回填给上游 AI。
- `docs/coding-agent-builtin-tools-implementation.md`：参考 Codex / DeepSeek-TUI / Claude Code / claude-code-tamagotchi 后，当前 Gateway 内置 coding-agent 工具兼容实现与缺口清单。
- `docs/gateway-admin-ui-config.md`：Python Gateway 管理 UI、上游配置、下游 key、MCP 配置入口、请求留存和调用频次/失败记录说明。
- `docs/api-tools-support-product-solution.md`：API Tools 支持功能的产品级完整方案，包括设置入口、自动测试、fallback adapter、Claude Code/Codex/OpenCode 工具调研和工具映射。
- `docs/native-tool-call-solution.md`：原生级 tools/function-call 网关方案，强调 native passthrough、能力探测和 fail-fast。
- `docs/hybrid-gateway-tool-orchestration.md`：早期 hybrid ownership 方案，保留作对照；当前以 full gateway runtime 方案为主。
- `docs/dialogue-curl-examples.md`：三类对话 API 的 curl 形态和 tools 原生格式。
- `docs/tool-function-call-shim.md`：早期 shim 草案，仅保留作历史对照；后续不应采用 prompt fake 方向。

## 推荐落地顺序

```text
Settings UI
  → Native capability probe
  → Provider capability registry
  → Native passthrough
  → Internal tool protocol adapter
  → Claude Code / Codex / OpenCode tool profiles
  → 权限与审计
```

## 验证

当前推荐用严格验证脚本一次性覆盖语法、单元、安全、功能 smoke 和并发压力：

```bash
./scripts/mimo_gateway.sh verify
```

也可以只跑基础回归：

```bash
python3 -m py_compile src/toolcall_gateway.py src/gateway_app.py src/gateway_builtin_tools.py tests/test_gateway.py tests/integration/*.py
python3 -m unittest discover -s tests -v
```

MCP 管理接口已包含：

```text
/admin/mcp-tools.json
/admin/mcp-health.json
/admin/mcp-health.json?probe=1
```

HTTP Action connector 已可把配置里的 HTTP endpoint 暴露为真实 tool/function executor：

```text
/admin/http-actions.json
```

## Claude Code / Codex / OpenCode 本地网关一键启动

公开版本不写入测试地址和真实 key；本地用环境变量或忽略的 `.gateway_service.json` 配置：

```text
UPSTREAM_BASE_URL=<YOUR_UPSTREAM_BASE_URL>
UPSTREAM_API_KEY=<YOUR_UPSTREAM_API_KEY>
UPSTREAM_MODEL=mimo-v2.5-pro
GATEWAY_DOWNSTREAM_KEY=<YOUR_DOWNSTREAM_KEY>
GATEWAY_ADMIN_PASSWORD=<YOUR_ADMIN_PASSWORD>
```

**关键环境变量：**
- `GATEWAY_DOWNSTREAM_KEY` - 下游 API Key（必填，生产环境勿使用默认值）
- `GATEWAY_ADMIN_PASSWORD` - 管理员密码（必填，生产环境勿使用默认值）
- `DOWNSTREAM_API_KEY` - 向下兼容的下游 Key（优先于 GATEWAY_DOWNSTREAM_KEY）

当前只保留两个用户脚本：

- `scripts/mimo_gateway.sh`：启动/停止本地 Gateway 服务 + Web 配置页面。默认监听 `0.0.0.0:8885`，本机客户端使用 `http://127.0.0.1:8885`。
- `scripts/claude_m1.sh`：一键启动/复用 Gateway，等待 `/healthz`，然后按你的 `claude_m1` 环境变量启动 `/usr/local/bin/claude --dangerously-skip-permissions`。 优先读取本地忽略的 `.gateway_service.json` 中的下游 key 和模型；没有本地配置时才使用公开示例 `local-gateway-key`。

启动本地 Gateway 服务：

```bash
cd /Users/sanbo/Desktop/ai_tool_functioncall
./scripts/mimo_gateway.sh start
```

或者直接启动 Claude Code：

```bash
cd /Users/sanbo/Desktop/ai_tool_functioncall
./scripts/claude_m1.sh
```

`claude_m1.sh` 等价于下面这组环境变量，并固定使用 `127.0.0.1` 给本机客户端连接：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8885"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export ANTHROPIC_AUTH_TOKEN="local-gateway-key"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_DEFAULT_OPUS_MODEL="mimo-v2.5-pro"
export ANTHROPIC_DEFAULT_SONNET_MODEL="mimo-v2.5-pro"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="mimo-v2.5-pro"
export ANTHROPIC_MODEL="mimo-v2.5-pro"
export ANTHROPIC_SMALL_FAST_MODEL="mimo-v2.5-pro"
export ENABLE_LSP_TOOL=1
/usr/local/bin/claude --dangerously-skip-permissions "$@"
```

服务脚本默认优先用 `screen + pidfile + healthz` 后台运行（无 screen 时回退 `nohup`）。

如果 8885 被占用，脚本默认会停止旧监听进程后重新启动；如需手工换端口：

```bash
GATEWAY_PORT=8886 ./scripts/mimo_gateway.sh start
GATEWAY_PORT=8886 ./scripts/claude_m1.sh
```

默认监听和下游 key：

```text
Base URL: http://127.0.0.1:8885
API Key: local-gateway-key
```

管理与配置页面：

```text
http://127.0.0.1:8885/ui
http://127.0.0.1:8885/client-config
```

默认管理员：

```text
admin / admin
```

Web UI 当前支持：

- 点击添加/编辑多个上游 API profile。
- 勾选上游能力：协议、streaming、tool call、function call、parallel tool calls、识图、网络、网络检索、JSON schema。
- 配置每个上游的路径映射：models / chat completions / responses / messages。
- 添加多个下游 key，并限制 key 可访问的协议：models、chat completions、responses、messages、direct tools/functions。
- 如果上游只支持 OpenAI `/v1/chat/completions`，Gateway 仍可让下游访问 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`，内部统一转换到上游 chat completions。

本地兼容请求示例：

```bash
curl http://127.0.0.1:8885/v1/models \
  -H "Authorization: Bearer local-gateway-key"

curl http://127.0.0.1:8885/v1/chat/completions \
  -H "Authorization: Bearer local-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-pro","messages":[{"role":"user","content":"Hello!"}]}'

curl http://127.0.0.1:8885/v1/chat/completions \
  -H "Authorization: Bearer local-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-pro","messages":[{"role":"user","content":"Hello!"}],"stream":true}'

curl http://127.0.0.1:8885/v1/responses \
  -H "Authorization: Bearer local-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-pro","input":[{"role":"user","content":"你好"}]}'

curl http://127.0.0.1:8885/v1/messages \
  -H "Authorization: Bearer local-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-pro","messages":[{"role":"user","content":"Hello!"}],"max_tokens":100}'
```

兼容点：

- `/v1/messages`：Claude Code 主路径，支持 Anthropic `tool_use/tool_result`。
- `/v1/messages/count_tokens`：本地估算，避免 Claude Code 预检查失败。
- `/v1/models`：透传上游模型列表；旧 key 即使没有显式 `models` 协议，也允许模型发现。
- 上游默认按 OpenAI chat completions 调用；responses/messages 下游请求会转换到上游 `/v1/chat/completions`。
- 管理页面可配置上游 base/model/key、协议路由、timeout、token 上限、并发、识图/网络/tool calls/function calls/parallel tool calls 等能力开关。
- 超长类/文件：`Read` 默认分块返回（默认 2000 行），返回里会提示下一次 `offset`；超大用户输入可走 context fan-out，启动脚本默认开启，`fanout_max_chunks=0` 表示不限制分片数量。

## 健康检查

```bash
curl http://127.0.0.1:8885/healthz
```

预期包含：

```json
{
  "ok": true,
  "mode": "orchestrate",
  "fake_prompt_tools": false
}
```

检查 UI：

```bash
curl -u admin:admin -i http://127.0.0.1:8885/ui
```

---

## 配置上游 API

可以在 UI 中配置：

```text
Base URL: 上游模型 API 地址
API Key: 上游 API key
Model: 默认模型
Protocol: openai_chat / openai_responses / anthropic_messages / openai_compatible
Tool Mode: orchestrate
Tools Enabled: auto 或 on
Timeout / Max Input Tokens / Max Output Tokens / Upstream Max Concurrency
Capabilities: streaming / vision / network / tool calls / function calls / parallel tool calls / JSON schema
Routes: models / chat_completions / responses / messages
Gateway Runtime: max concurrent requests / queue timeout / tool timeout / unsupported tool recording
Context Router: max input tokens / fanout chunk tokens / fanout max chunks / fanout max workers
```

也可以通过环境变量启动：

```bash
UPSTREAM_BASE_URL="http://127.0.0.1:8000" \
UPSTREAM_API_KEY="your-upstream-key" \
UPSTREAM_MODEL="your-model" \
python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8885
```

默认配置文件：

```text
.gateway_service.json
```

---

## 下游客户端接入

Codex / Claude Code / DeepSeek-TUI / OpenCode / SDK 使用一键脚本时的通用配置：

```text
Base URL: http://127.0.0.1:8885
API Key: local-gateway-key
```

如果你手工改了 `GATEWAY_PORT` 或 `DOWNSTREAM_API_KEY`，客户端配置也要同步修改。

支持接口：

```text
/v1/chat/completions
/v1/responses
/v1/messages
/v1/tools/call
/v1/functions/call
/tools/call
```

协议转换：如果上游只有 OpenAI `/v1/chat/completions`，在 UI 里把 `Protocol` 设为 `openai_chat`，`Chat Completions Path` 设为 `/v1/chat/completions` 即可。下游仍然可以调用 Chat Completions / Responses / Anthropic Messages 三种协议，Gateway 会统一转成上游 Chat Completions 请求，并把结果转换回下游需要的响应格式。

流式与非流式：

- `stream: false`：Gateway 正常编排 tool/function-call 多轮调用后返回最终 JSON。
- `stream: true` + `tool_mode=orchestrate`：Gateway 内部用非流式上游完成真实工具编排，再向下游输出对应协议的 SSE，避免伪造中间 tool event。
- `stream: true` + `tool_mode=passthrough/native_passthrough/proxy`：直接透传上游 SSE，适合下游客户端主要以流式调用的场景。
- 优先级：上游能稳定返回原生 `tool_calls` / `tool_use` 时使用上游原生协议；如果上游退化成文本 `<function=Glob>` / `<parameter=pattern>`，Gateway 会识别这种 Claude-Code-like 标记，调用本地真实工具，再把结果回填给上游继续生成最终答案。
- 超长 Claude Code 请求会先做 compaction：移除下游巨大 tool schema / metadata / thinking / output_config，替换成 Gateway 精简 system 指令，再由 Gateway 重新暴露自己的工具，避免上游直接返回 “text too long”。

直接 Tool / Function 调用：

- `/v1/tools/call`、`/v1/functions/call`、`/tools/call` 可以不经过上游模型，直接调用 Gateway 真实工具 runtime。
- 兼容常见输入形态：
  - OpenAI function：`{"function":{"name":"calculator","arguments":"{\"expression\":\"1+1\"}"}}`
  - Anthropic tool_use：`{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file":"README.md"}}`
  - Gateway/MCP：`{"name":"mcp__server__tool","arguments":{...}}` 或 `{"tool_name":"Read","input":{...}}`
- 返回同时带 `content`、`openai_chat`、`openai_responses`、`anthropic` 三种回填片段，方便 Claude Code / Codex / OpenCode / DeepSeek-TUI 直接接入。

已尽量真实实现的内置工具：

- 文件/代码：`Read`/`view`/`read_file`、`LS`、`Glob`、`Grep`/`file_search`、`Write`、`Edit`/`str_replace_editor`、`MultiEdit`、`NotebookEdit`、`apply_patch`。
- 执行/编排：`Bash`/`exec_command`/`shell`、`multi_tool_use.parallel`、`update_plan`、`TodoWrite`、`ExitPlanMode`/`EnterPlanMode`。
- 网络/资源：`WebFetch`/`web_fetch`、`WebSearch`/`web_search`、`view_image`。
- MCP：`mcp__server__tool`、DeepSeek-TUI 风格 `mcp_server_tool`、`list_mcp_resources`、`list_mcp_resource_templates`、`read_mcp_resource`/`mcp_read_resource`、`mcp_get_prompt`。
- 已实现常见 Agent/Skill/Memory/代码解释器兼容入口；仍需要外部 runtime 的能力（例如真实电脑控制 `click/type_text/press_key/scroll/computer_use`、图像生成等）会明确返回 `connector_required`，不要伪造成成功；建议通过 MCP 市场安装相应 MCP server 或配置 HTTP Action 补全。

上下文分流：

- 默认开启；也可在管理 UI 的 `Context Router / 分流压缩` 调整或关闭。
- 超过 `Max Input Tokens` 且开启 `fanout` 时，Gateway 会把最后一条超大用户输入拆成多个子请求分别分析，再发起一次综合请求返回最终答案，适合“分析 N 个类 / 多文件内容后汇总”的场景。
- 为避免重复执行有副作用工具，强制 `tool_choice` 的请求不会触发 fan-out；子请求会移除 tools，只做文本分析与综合。
- fan-out 默认会再跑一次质量审查请求，要求“语义分析 -> 调用/证据检查 -> 反思调整 -> 最终结论”，避免只是机械拆分和简单汇总。

认证方式：

```text
Authorization: Bearer local-gateway-key
```

或：

```text
x-api-key: local-gateway-key
```

---

## Curl 调用示例

### OpenAI Chat Completions

```bash
curl http://127.0.0.1:8885/v1/chat/completions \
  -H 'authorization: Bearer local-gateway-key' \
  -H 'content-type: application/json' \
  -d @examples/chat-with-tool.json
```

### OpenAI Responses

```bash
curl http://127.0.0.1:8885/v1/responses \
  -H 'authorization: Bearer local-gateway-key' \
  -H 'content-type: application/json' \
  -d @examples/responses-with-tool.json
```

### Anthropic Messages

```bash
curl http://127.0.0.1:8885/v1/messages \
  -H 'authorization: Bearer local-gateway-key' \
  -H 'content-type: application/json' \
  -d @examples/messages-with-tool.json
```

### 直接调用 Tool / Function

```bash
curl http://127.0.0.1:8885/v1/tools/call \
  -H 'authorization: Bearer local-gateway-key' \
  -H 'content-type: application/json' \
  -d '{"function":{"name":"calculator","arguments":"{\"expression\":\"20+22\"}"},"call_id":"call_1"}'
```

```bash
curl http://127.0.0.1:8885/v1/tools/call \
  -H 'authorization: Bearer local-gateway-key' \
  -H 'content-type: application/json' \
  -d '{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file":"README.md"}}'
```

如果没有配置上游 API，模型调用会返回上游连接错误，这是正常的。要做不依赖真实模型的完整闭环测试，看下面的 Fake Upstream 测试。

---

## MCP 测试方法

查看 MCP 健康状态：

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-health.json
```

主动 probe：

```bash
curl -u admin:admin 'http://127.0.0.1:8885/admin/mcp-health.json?probe=1'
```

查看 MCP tools：

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-tools.json
```

MCP 配置示例，可以填到 UI 的 MCP 配置区域：

```json
[
  {
    "name": "github",
    "type": "mcp_stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": ["GITHUB_TOKEN"],
    "cwd": ".",
    "enabled": true,
    "pool": true,
    "catalog_ttl": 60
  }
]
```

成功后，MCP tools 会以这个格式暴露：

```text
mcp__github__<tool_name>
```

---

## HTTP Action 测试方法

HTTP Action 可以把已有 HTTP 服务包装成真实 tool/function executor。

### 启动本地测试 action 服务

新开终端：

```bash
python3 - <<'PY'
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('content-length') or '0')
        body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        payload = json.dumps({'ok': True, 'received': body}, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        return

ThreadingHTTPServer(('127.0.0.1', 9000), Handler).serve_forever()
PY
```

### 在 UI 配置 HTTP Action

在 HTTP Actions 区域填入：

```json
[
  {
    "name": "echo_http",
    "description": "Echo input through local HTTP action",
    "method": "POST",
    "url": "http://127.0.0.1:9000/echo",
    "input_schema": {
      "type": "object",
      "properties": {
        "value": {"type": "string"}
      },
      "required": ["value"]
    },
    "enabled": true
  }
]
```

查看配置：

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/http-actions.json
```

---

## 本地完整闭环测试：Fake Upstream + Gateway

这个测试不依赖真实模型 API。Fake upstream 第一次返回协议级 `tool_calls`，Gateway 执行 `calculator` 后再次请求 upstream，upstream 返回最终回答。

### 1. 启动 fake upstream

新开终端：

```bash
python3 - <<'PY'
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        Handler.calls += 1
        length = int(self.headers.get('content-length') or '0')
        body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        if Handler.calls == 1:
            payload = {
                'choices': [{
                    'message': {
                        'role': 'assistant',
                        'content': None,
                        'tool_calls': [{
                            'id': 'call_1',
                            'type': 'function',
                            'function': {
                                'name': 'calculator',
                                'arguments': json.dumps({'expression': '123*456+7'})
                            }
                        }]
                    },
                    'finish_reason': 'tool_calls'
                }]
            }
        else:
            tool_result = body['messages'][-1]['content']
            payload = {
                'choices': [{
                    'message': {
                        'role': 'assistant',
                        'content': '最终结果是 ' + str(tool_result)
                    },
                    'finish_reason': 'stop'
                }]
            }
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return

ThreadingHTTPServer(('127.0.0.1', 18080), Handler).serve_forever()
PY
```

### 2. 启动 Gateway 指向 fake upstream

新开终端：

```bash
cd /Users/sanbo/Desktop/ai_tool_functioncall
UPSTREAM_BASE_URL='http://127.0.0.1:18080' \
UPSTREAM_MODEL='fake-model' \
python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8885
```

如果 `.gateway_service.json` 已经保存过其他上游地址，请在 UI 里把 Base URL 改成：

```text
http://127.0.0.1:18080
```

### 3. 发送请求

```bash
curl http://127.0.0.1:8885/v1/chat/completions \
  -H 'authorization: Bearer local-gateway-key' \
  -H 'content-type: application/json' \
  -d '{
    "model": "fake-model",
    "messages": [
      {"role": "user", "content": "计算 123*456+7"}
    ]
  }'
```

预期最终响应包含：

```text
最终结果是 56095
```

这证明链路是真实执行：

```text
下游请求 -> Gateway -> 上游返回 tool_calls -> Gateway 执行 calculator -> Gateway 回填 tool result -> 上游返回最终回答
```

---

## 自动化测试

一键严格验证：

```bash
./scripts/mimo_gateway.sh verify
```

分开执行：

```bash
python3 -m py_compile src/toolcall_gateway.py src/gateway_app.py src/gateway_builtin_tools.py tests/test_gateway.py tests/integration/*.py
python3 -m unittest discover -s tests -v
python3 -W error::ResourceWarning -m unittest discover -s tests -v
./tests/integration/security_gateway_checks.py
./tests/integration/smoke_gateway_tools.py
./tests/integration/stress_gateway_concurrency.py --workers 16 --direct-tool-requests 32 --model-requests 1
```

当前单元测试预期：

```text
以当前测试输出为准；严格验收重点是 `CORE TOOL ACCEPTANCE` 通过
OK
```

---

## 常用管理接口

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/config.json
curl -u admin:admin http://127.0.0.1:8885/admin/stats.json
curl -u admin:admin http://127.0.0.1:8885/admin/requests.json
curl -u admin:admin http://127.0.0.1:8885/admin/failures.json
curl -u admin:admin http://127.0.0.1:8885/admin/tools.json
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-tools.json
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-health.json
curl -u admin:admin http://127.0.0.1:8885/admin/http-actions.json
```

---

## 日志和数据文件

```text
.gateway_service.json        # Gateway 服务配置
gateway_log.sqlite3         # 默认日志库；SQLite + WAL，保存请求、失败 tool、统计
.gateway_requests.jsonl       # 旧日志格式，仅作为历史导入/兼容读取，不再默认写入
.gateway_stats.json           # 旧统计格式，仅作为历史导入/兼容读取，不再默认写入
.gateway_tool_failures.jsonl  # 旧失败日志格式，仅作为历史导入/兼容读取，不再默认写入
```

---

### 文本工具调用容错

Gateway 除了识别 `<function=Tool>` / `<parameter=name>`，也兼容弱上游漏掉 `<function=Bash>`、只输出多个 `<parameter=command>` 的情况。对常见空格丢失形态会做保守修复，例如：

```text
find/Users/... -typef -name '.py' | head-30
find ... -exec wc -l{} + |sort -n| tail -20
```

会修复为可执行的 Bash 命令后再调用真实本地 shell。

如果上游返回 “text too long / send it in parts / 内容过长” 这类上下文拒绝，Gateway 会触发 forced fan-out，把原始请求拆片分析、综合后再返回，避免直接把上游拒绝原样返回给 Claude Code。

## 常见问题

### 返回 401

下游请求没有带 key，或者 key 不对。

正确示例：

```bash
-H 'authorization: Bearer local-gateway-key'
```

### 返回 upstream connection failed

没有配置上游 API，或者上游不可访问。

检查：

```bash
curl http://127.0.0.1:8885/healthz
curl -u admin:admin http://127.0.0.1:8885/admin/config.json
```

### tool_not_found

模型调用了 Gateway 没有实现、也没有通过 MCP/HTTP Action 配置的工具。

查看失败：

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/failures.json
```

### connector_required

该工具名已被识别为常见 coding-agent 工具，但当前还需要外部 connector/runtime。

这类失败会被记录下来，作为后续持续维护和扩展的 backlog。

### 写文件或 Shell 工具失败

默认关闭写入和 shell 权限。需要在 UI 打开：

```text
允许写入工具
允许 Shell 工具
```

### 无限上下文实现反思

最新修正：forced fan-out 不能在综合阶段重新塞回完整原文，否则会二次触发上游 `too long`。现在综合/质量审查阶段只携带：

- 原始问题的压缩摘要。
- 预算内裁剪后的子分析结果。
- forced 模式下更小的片段 token 预算。
- 文本工具回退会把 `Read`/`FileInfo` 等路径参数从弱模型输出的 Markdown/正文噪声里提取成单一路径，避免把整段报告当文件名造成 `File name too long`。

因此路径变为：超大请求或上游 too-long 拒绝 -> 小片段并发子分析 -> 预算内综合 -> 预算内质量审查 -> 最终答案。
