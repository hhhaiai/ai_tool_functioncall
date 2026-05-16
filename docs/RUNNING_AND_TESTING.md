# Gateway 运行与测试 README

本文档用于从零启动、配置、调用和验证 `API Tools / Function Call Gateway`。当前实现是 Python 标准库版本，不需要安装第三方依赖。

## 1. 进入项目目录

```bash
cd /Users/sanbo/Desktop/ai_tool_functioncall
```

确认 Python 版本：

```bash
python3 --version
```

建议 Python 3.10+。

---

## 2. 启动 Gateway

默认端口：`8885`。

```bash
./scripts/mimo_gateway.sh start
```

启动后访问管理 UI：

```text
http://127.0.0.1:8885/ui
```

默认管理员账号：

```text
admin / admin（开发/测试用默认值，生产环境必须通过环境变量修改）
```

默认下游 API Key：

```text
无默认值，必须通过环境变量或配置文件设置
```

> **重要**：生产环境必须设置 `GATEWAY_ADMIN_PASSWORD`（管理员密码）和 `GATEWAY_DOWNSTREAM_KEY`（下游 API Key）。开发/测试环境可使用默认值 `admin/admin`，但必须在上线前修改。

---

## 3. 基础健康检查

新开一个终端执行：

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

检查 Admin UI：

```bash
curl -u admin:admin -i http://127.0.0.1:8885/ui
```

预期：

```text
HTTP/1.0 200 OK
```

---

## 4. 配置上游 API

在 UI 中配置：

```text
Base URL: 上游模型 API 地址，例如 http://127.0.0.1:8000
API Key: 上游 API key
Model: 默认模型
Protocol: openai_chat / openai_responses / anthropic_messages / openai_compatible
Tool Mode: orchestrate
Tools Enabled: auto 或 on
```

也可以通过环境变量初始化：

```bash
UPSTREAM_BASE_URL="http://127.0.0.1:8000" \
UPSTREAM_API_KEY="your-upstream-key" \
UPSTREAM_MODEL="your-model" \
GATEWAY_DOWNSTREAM_KEY="your-downstream-key" \
GATEWAY_ADMIN_PASSWORD="your-admin-password" \
./scripts/mimo_gateway.sh start
```

**关键环境变量：**

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `GATEWAY_DOWNSTREAM_KEY` | 下游 API Key（必填） | 无 |
| `GATEWAY_ADMIN_PASSWORD` | 管理员密码（必填） | `admin`（仅开发环境） |
| `UPSTREAM_BASE_URL` | 上游 API 地址 | 无 |
| `UPSTREAM_API_KEY` | 上游 API Key | 无 |
| `UPSTREAM_MODEL` | 默认模型 | `mimo-v2.5-pro` |
| `DOWNSTREAM_API_KEY` | 向下兼容的下游 Key（优先于 GATEWAY_DOWNSTREAM_KEY） | 无 |

配置文件默认保存到：

```text
.gateway_service.json
```

---

## 5. 下游客户端如何接入

下游客户端包括 Codex、Claude Code、DeepSeek-TUI、OpenCode 或普通 SDK。

通用配置：

```text
Base URL: http://127.0.0.1:8885
API Key: <GATEWAY_DOWNSTREAM_KEY 设置的值>
```

支持接口：

```text
/v1/chat/completions
/v1/responses
/v1/messages
```

认证方式任选一种：

```text
Authorization: Bearer <GATEWAY_DOWNSTREAM_KEY>
```

或：

```text
x-api-key: <GATEWAY_DOWNSTREAM_KEY>
```

---

## 6. Curl 调用示例

### 6.1 OpenAI Chat Completions

```bash
curl http://127.0.0.1:8885/v1/chat/completions \
  -H 'authorization: Bearer <YOUR_DOWNSTREAM_KEY>' \
  -H 'content-type: application/json' \
  -d @examples/chat-with-tool.json
```

### 6.2 OpenAI Responses

```bash
curl http://127.0.0.1:8885/v1/responses \
  -H 'authorization: Bearer <YOUR_DOWNSTREAM_KEY>' \
  -H 'content-type: application/json' \
  -d @examples/responses-with-tool.json
```

### 6.3 Anthropic Messages

```bash
curl http://127.0.0.1:8885/v1/messages \
  -H 'authorization: Bearer <YOUR_DOWNSTREAM_KEY>' \
  -H 'content-type: application/json' \
  -d @examples/messages-with-tool.json
```

如果没有配置上游 API，以上模型调用会返回上游连接相关错误，这是正常的。完整本地闭环测试见第 9 节。

---

## 7. 内置工具与权限

当前 Gateway 会把内置工具合并到请求里，让上游模型可以通过协议级 tool/function call 调用。

常用内置工具：

```text
calculator
get_current_time
Read / LS / Glob / Grep
Write / Edit / MultiEdit / apply_patch
Bash / exec_command
WebFetch
TodoWrite
update_plan
```

安全默认值：

```text
写文件工具默认关闭
Shell 工具默认关闭
```

如需启用，推荐在 UI 中打开：

```text
允许写入工具
允许 Shell 工具
```

或用环境变量：

```bash
GATEWAY_ALLOW_WRITE_TOOLS=1 \
GATEWAY_ALLOW_SHELL_TOOLS=1 \
./scripts/mimo_gateway.sh start
```

---

## 8. MCP 测试方法

### 8.1 查看 MCP 健康状态

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-health.json
```

主动 probe：

```bash
curl -u admin:admin 'http://127.0.0.1:8885/admin/mcp-health.json?probe=1'
```

### 8.2 查看 MCP tools

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-tools.json
```

### 8.3 MCP 配置示例

在 UI 的 MCP 配置区域填入：

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

成功后，MCP tools 会以如下格式暴露：

```text
mcp__github__<tool_name>
```

如果 MCP 启动失败或调用失败，Gateway 会：

1. 关闭对应 session。
2. 清理 catalog cache。
3. 标记状态为 `broken`。
4. 记录失败日志。
5. 下次调用时重新启动 session。

---

## 9. HTTP Action 测试方法

HTTP Action 用于把已有 HTTP 服务包装成真实 tool/function executor。

### 9.1 启动一个本地测试 action 服务

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

### 9.2 在 Gateway UI 配置 HTTP Action

进入：

```text
http://127.0.0.1:8885/ui
```

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

保存后查看：

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/http-actions.json
```

预期包含：

```json
{"name":"echo_http"}
```

### 9.3 真实执行 HTTP Action

HTTP Action 需要上游模型返回协议级 tool/function call 才会被 Gateway 执行。完整闭环可以使用第 10 节的 fake upstream 测试。

---

## 10. 本地完整闭环测试：Fake Upstream + Gateway

这个测试不依赖真实模型 API，用一个 fake upstream 模拟模型第一次返回 `tool_calls`，Gateway 执行工具后再次请求 upstream，upstream 返回最终回答。

### 10.1 启动 fake upstream

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

### 10.2 使用 fake upstream 启动 Gateway

新开终端：

```bash
cd /Users/sanbo/Desktop/ai_tool_functioncall
UPSTREAM_BASE_URL='http://127.0.0.1:18080' \
UPSTREAM_MODEL='fake-model' \
./scripts/mimo_gateway.sh start
```

如果 `.gateway_service.json` 里已经保存了其他上游地址，请在 UI 里把 Base URL 改为：

```text
http://127.0.0.1:18080
```

### 10.3 发送请求

```bash
curl http://127.0.0.1:8885/v1/chat/completions \
  -H 'authorization: Bearer <YOUR_DOWNSTREAM_KEY>' \
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

这证明：

```text
下游请求 -> Gateway -> 上游返回 tool_calls -> Gateway 执行 calculator -> Gateway 回填 tool result -> 上游返回最终回答
```

整个过程是真实协议级 tool call，不是 prompt fake。

---

## 11. 自动化测试

### 11.1 一键严格验证

```bash
./scripts/mimo_gateway.sh verify
```

该命令会依次执行：语法检查、单元测试、核心 tools acceptance、安全/auth guardrails、真实工具 smoke、并发压力。

### 11.2 分开执行

```bash
python3 -m py_compile src/toolcall_gateway.py src/gateway_app.py src/gateway_builtin_tools.py tests/test_gateway.py tests/integration/*.py
python3 -m unittest discover -s tests -v
python3 -W error::ResourceWarning -m unittest discover -s tests -v
./tests/integration/tool_acceptance.py
./tests/integration/security_gateway_checks.py
./tests/integration/smoke_gateway_tools.py
./tests/integration/stress_gateway_concurrency.py --workers 16 --direct-tool-requests 32 --model-requests 1
```

当前预期以实际输出为准；核心验收必须看到 `CORE TOOL ACCEPTANCE` / `acceptance: tools` 通过。

测试覆盖：

- Chat Completions `tool_calls` 提取和结果回填。
- Responses API `function_call` 提取和结果回填。
- Anthropic Messages `tool_use` 提取和结果回填。
- 强制 tool_choice 的 native 校验。
- 内置工具 registry。
- `calculator` 执行。
- `Read` / `Glob` / `Grep` workspace root 限制。
- Gateway 多轮 tool orchestration。
- stdio MCP `initialize` / `tools/list` / `tools/call`。
- MCP schema merge。
- MCP broken server health 和 cache invalidation。
- `/admin/mcp-health.json?probe=1`。
- HTTP Action schema 暴露和真实 HTTP 执行。

---

## 12. 常用管理接口

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/config.json
curl -u admin:admin http://127.0.0.1:8885/admin/stats.json
curl -u admin:admin http://127.0.0.1:8885/admin/requests.json
curl -u admin:admin http://127.0.0.1:8885/admin/failures.json
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-tools.json
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-health.json
curl -u admin:admin http://127.0.0.1:8885/admin/http-actions.json
```

---

## 13. 日志和数据文件

```text
.gateway_service.json        # Gateway 服务配置
gateway_log.sqlite3         # 默认日志库；SQLite + WAL，保存请求、失败 tool、统计
.gateway_requests.jsonl       # 旧日志格式，仅历史导入/兼容读取，不再默认写入
.gateway_stats.json           # 旧统计格式，仅历史导入/兼容读取，不再默认写入
.gateway_tool_failures.jsonl  # 旧失败日志，仅历史导入/兼容读取，不再默认写入
```

其中 `gateway_log.sqlite3` 是默认高频写入路径，用于：

1. 复现下游请求。
2. 分析 Codex / Claude Code / DeepSeek-TUI / OpenCode 的真实调用行为。
3. 统计 tool 使用频次。
4. 记录失败和不支持的 tool/function call。
5. 后续接入 MCP/OpenAPI/action/plugin marketplace 并持续增强。

隐藏 JSONL/JSON 文件只作为历史兼容读取，不作为默认写入路径。

---

## 14. 常见问题

### 14.1 返回 401

下游请求没有带 key，或者 key 不对。

正确示例：

```bash
-H 'authorization: Bearer <YOUR_DOWNSTREAM_KEY>'
```

### 14.2 返回 upstream connection failed

没有配置上游 API，或者上游不可访问。

检查：

```bash
curl http://127.0.0.1:8885/healthz
curl -u admin:admin http://127.0.0.1:8885/admin/config.json
```

### 14.3 tool_not_found

模型调用了 Gateway 没有实现、也没有通过 MCP/HTTP Action 配置的工具。

查看失败：

```bash
curl -u admin:admin http://127.0.0.1:8885/admin/failures.json
```

后续处理：

1. 实现内置工具。
2. 配置 MCP server。
3. 配置 HTTP Action。
4. 后续接入 OpenAPI/action/plugin marketplace。

### 14.4 connector_required

该工具名已被识别为常见 coding-agent 工具，但当前还需要外部 connector/runtime。

这类失败会被记录下来，作为后续持续维护和扩展的 backlog。

### 14.5 写文件或 Shell 工具失败

默认关闭写入和 shell 权限。需要在 UI 打开：

```text
允许写入工具
允许 Shell 工具
```

或设置：

```bash
GATEWAY_ALLOW_WRITE_TOOLS=1
GATEWAY_ALLOW_SHELL_TOOLS=1
```

### Tool markup / too-long 回归

已覆盖两类容易导致 Claude Code 卡住的回归：

1. 只有 `<parameter=command>`、没有 `<function=Bash>` 的文本工具调用：Gateway 会推断为 Bash，并修复 `find/Users`、`-typef`、`wc -l{}`、`head-30` 等常见空格丢失问题。
2. 上游返回 `Sorry, the text you sent is too long` 或类似上下文拒绝：Gateway 会 forced fan-out，而不是把拒绝文本当最终答案返回。

对应单测：

```bash
python3 -m unittest discover -s tests -p test_gateway.py -k 'parameter_only_bash_markup_repairs_missing_spaces' -v
python3 -m unittest discover -s tests -p test_gateway.py -k 'upstream_too_long_response_triggers_forced_fanout' -v
```
