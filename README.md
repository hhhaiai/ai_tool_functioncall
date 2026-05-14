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

当前已有 native-first/fail-fast 方向的基础测试：

```bash
python3 -m py_compile src/toolcall_gateway.py tests/test_gateway.py
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
---

## 快速运行

进入项目目录：

```bash
cd /Users/sanbo/Desktop/ai_tool_functioncall
```

启动 Gateway：

```bash
python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8787
```

管理 UI：

```text
http://127.0.0.1:8787/ui
```

默认管理员：

```text
admin / admin
```

默认下游 API Key：

```text
dev-gateway-key
```

生产或长期使用前，请在 UI 中修改管理员密码，并新增正式下游 key。

---

## 健康检查

```bash
curl http://127.0.0.1:8787/healthz
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
curl -u admin:admin -i http://127.0.0.1:8787/ui
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
```

也可以通过环境变量启动：

```bash
UPSTREAM_BASE_URL="http://127.0.0.1:8000" \
UPSTREAM_API_KEY="your-upstream-key" \
UPSTREAM_MODEL="your-model" \
python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8787
```

默认配置文件：

```text
.gateway_config.json
```

---

## 下游客户端接入

Codex / Claude Code / DeepSeek-TUI / OpenCode / SDK 通用配置：

```text
Base URL: http://127.0.0.1:8787
API Key: dev-gateway-key
```

支持接口：

```text
/v1/chat/completions
/v1/responses
/v1/messages
```

认证方式：

```text
Authorization: Bearer dev-gateway-key
```

或：

```text
x-api-key: dev-gateway-key
```

---

## Curl 调用示例

### OpenAI Chat Completions

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'authorization: Bearer dev-gateway-key' \
  -H 'content-type: application/json' \
  -d @examples/chat-with-tool.json
```

### OpenAI Responses

```bash
curl http://127.0.0.1:8787/v1/responses \
  -H 'authorization: Bearer dev-gateway-key' \
  -H 'content-type: application/json' \
  -d @examples/responses-with-tool.json
```

### Anthropic Messages

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'authorization: Bearer dev-gateway-key' \
  -H 'content-type: application/json' \
  -d @examples/messages-with-tool.json
```

如果没有配置上游 API，模型调用会返回上游连接错误，这是正常的。要做不依赖真实模型的完整闭环测试，看下面的 Fake Upstream 测试。

---

## MCP 测试方法

查看 MCP 健康状态：

```bash
curl -u admin:admin http://127.0.0.1:8787/admin/mcp-health.json
```

主动 probe：

```bash
curl -u admin:admin 'http://127.0.0.1:8787/admin/mcp-health.json?probe=1'
```

查看 MCP tools：

```bash
curl -u admin:admin http://127.0.0.1:8787/admin/mcp-tools.json
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
curl -u admin:admin http://127.0.0.1:8787/admin/http-actions.json
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
python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8787
```

如果 `.gateway_config.json` 已经保存过其他上游地址，请在 UI 里把 Base URL 改成：

```text
http://127.0.0.1:18080
```

### 3. 发送请求

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'authorization: Bearer dev-gateway-key' \
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

语法检查：

```bash
python3 -m py_compile src/toolcall_gateway.py tests/test_gateway.py
```

单元测试：

```bash
python3 -m unittest discover -s tests -v
```

严格资源泄漏测试：

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

当前预期：

```text
Ran 17 tests
OK
```

---

## 常用管理接口

```bash
curl -u admin:admin http://127.0.0.1:8787/admin/config.json
curl -u admin:admin http://127.0.0.1:8787/admin/stats.json
curl -u admin:admin http://127.0.0.1:8787/admin/requests.json
curl -u admin:admin http://127.0.0.1:8787/admin/failures.json
curl -u admin:admin http://127.0.0.1:8787/admin/mcp-tools.json
curl -u admin:admin http://127.0.0.1:8787/admin/mcp-health.json
curl -u admin:admin http://127.0.0.1:8787/admin/http-actions.json
```

---

## 日志和数据文件

```text
.gateway_config.json          # Gateway 配置
.gateway_requests.jsonl       # 下游请求/响应留存
.gateway_stats.json           # tool 调用统计
.gateway_tool_failures.jsonl  # tool/function 失败记录
```

---

## 常见问题

### 返回 401

下游请求没有带 key，或者 key 不对。

正确示例：

```bash
-H 'authorization: Bearer dev-gateway-key'
```

### 返回 upstream connection failed

没有配置上游 API，或者上游不可访问。

检查：

```bash
curl http://127.0.0.1:8787/healthz
curl -u admin:admin http://127.0.0.1:8787/admin/config.json
```

### tool_not_found

模型调用了 Gateway 没有实现、也没有通过 MCP/HTTP Action 配置的工具。

查看失败：

```bash
curl -u admin:admin http://127.0.0.1:8787/admin/failures.json
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
