# API Tools / Function Call Gateway

让 `/v1/chat/completions`、`/v1/responses`、`/v1/messages` 以**可验证的方式**支持 tools / function call / tool use。

```text
可以 adapter，但不能 fake。
可以 fallback，但 fallback 必须连接真实工具 runtime 或真实 native-tools provider。
```

---

## 核心特性

| 特性 | 说明 |
|------|------|
| 真实工具执行 | 内置工具 + MCP + HTTP Action，无伪造 |
| 多协议支持 | OpenAI Chat / Responses / Anthropic Messages |
| 零依赖 | 纯 Python 标准库，跨平台 |
| 内置工具 | 文件读写、Shell、搜索、Web 等 20+ 工具 |
| 上下文管理 | 自动压缩、扇出、记忆 |
| Admin UI | Web 配置界面，实时管理 |

---

## 快速开始

```bash
# 1. 克隆
git clone <repo> && cd ai_tool-functioncall

# 2. 配置
cp gateway.config.json .gateway_service.json
vi .gateway_service.json  # 填入 upstream.base_url, upstream.api_key, upstream.model

# 3. 启动
./scripts/mimo_gateway.sh start

# 4. 验证
curl http://127.0.0.1:8885/healthz
```

**默认访问地址**

```text
Admin UI:  http://127.0.0.1:8885/ui
管理员:    admin / admin
API Key:   见 .gateway_service.json 中的 client_snippet_api_key
```

---

## 客户端接入

### Claude Code

```bash
./scripts/claude_m1.sh

# 或手动配置
export ANTHROPIC_BASE_URL="http://127.0.0.1:8885"
export ANTHROPIC_API_KEY="your-gateway-key"
claude
```

### OpenCode / Codex

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8885/v1"
export OPENAI_API_KEY="your-gateway-key"
opencode  # 或 codex
```

### Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8885/v1", api_key="your-key")
```

---

## 配置说明

### 配置文件

```text
gateway.config.json      # 模板（入 git）
.gateway_service.json    # 本地配置（不入 git，优先级高）
```

### 关键配置项

```json
{
  "upstream": {
    "base_url": "https://api.openai.com",
    "api_key": "sk-xxx",
    "model": "gpt-4o"
  },
  "gateway": {
    "workspace_root": "/path/to/project",
    "client_snippet_api_key": "your-gateway-key",
    "allow_write_tools": true,
    "allow_shell_tools": true
  }
}
```

### 环境变量（优先级最高）

| 变量 | 说明 |
|------|------|
| `UPSTREAM_BASE_URL` | 上游 API 地址 |
| `UPSTREAM_API_KEY` | 上游 API Key |
| `UPSTREAM_MODEL` | 默认模型 |
| `GATEWAY_DOWNSTREAM_KEY` | 下游 API Key |
| `GATEWAY_ADMIN_PASSWORD` | 管理员密码 |
| `GATEWAY_PORT` | 监听端口（默认 8885） |

详见 [docs/RUNNING_AND_TESTING.md](docs/RUNNING_AND_TESTING.md)

---

## 内置工具

| 类别 | 工具 |
|------|------|
| 文件 | Read, Write, Edit, MultiEdit, LS, Glob, Grep |
| 执行 | Bash, Shell |
| 网络 | WebFetch, WebSearch |
| Agent | TodoWrite, update_plan, ExitPlanMode |
| MCP | mcp__server__tool, list_mcp_resources |
| 计算 | calculator |

---

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/healthz` | GET | 健康检查 |
| `/ui` | GET | Admin UI |
| `/v1/models` | GET | 模型列表 |
| `/v1/chat/completions` | POST | OpenAI Chat |
| `/v1/responses` | POST | OpenAI Responses |
| `/v1/messages` | POST | Anthropic Messages |
| `/v1/tools/call` | POST | 直接工具调用 |

---

## 测试

```bash
# 单元测试（97 个）
python3 -m unittest discover -s tests -v

# 集成测试
./scripts/mimo_gateway.sh verify

# Fake Upstream 闭环测试
# 见下方 "本地完整闭环测试" 章节
```

---

## 部署方式

### 开发环境

```bash
./scripts/mimo_gateway.sh start
```

### 生产环境 - systemd

```bash
# 见 docs/RUNNING_AND_TESTING.md 第 7 章
sudo systemctl start gateway
```

### 生产环境 - Docker

```bash
docker build -t gateway .
docker run -d -p 8885:8885 gateway
```

---

## 本地完整闭环测试

不依赖真实模型 API 的测试方法：

### 1. 启动 Fake Upstream

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

### 2. 启动 Gateway

```bash
UPSTREAM_BASE_URL='http://127.0.0.1:18080' \
UPSTREAM_MODEL='fake-model' \
python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8885
```

### 3. 发送请求

```bash
curl http://127.0.0.1:8885/v1/chat/completions \
  -H 'authorization: Bearer your-key' \
  -H 'content-type: application/json' \
  -d '{"model":"fake-model","messages":[{"role":"user","content":"计算 123*456+7"}]}'
```

预期返回：`最终结果是 56095`

---

## 管理接口

```bash
# 配置
curl -u admin:admin http://127.0.0.1:8885/admin/config.json

# 统计
curl -u admin:admin http://127.0.0.1:8885/admin/stats.json

# 请求日志
curl -u admin:admin http://127.0.0.1:8885/admin/requests.json

# 失败记录
curl -u admin:admin http://127.0.0.1:8885/admin/failures.json

# MCP 工具
curl -u admin:admin http://127.0.0.1:8885/admin/mcp-tools.json

# HTTP Actions
curl -u admin:admin http://127.0.0.1:8885/admin/http-actions.json
```

---

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 401 | Key 错误 | 检查 Authorization header |
| upstream connection failed | 上游未配置 | 检查 upstream.base_url |
| tool_not_found | 工具未实现 | 查看 /admin/failures.json |
| connector_required | 需要外部 runtime | 配置 MCP 或 HTTP Action |
| 写文件/Shell 失败 | 权限未开 | UI 中开启 allow_write/allow_shell |

---

## 项目结构

```
ai_tool_functioncall/
├── src/
│   ├── toolcall_gateway.py      # 入口
│   ├── gateway_app.py           # 核心逻辑
│   ├── gateway_builtin_tools.py # 内置工具
│   ├── gateway_streaming.py     # SSE 处理
│   ├── gateway_tool_runtime.py  # 工具运行时
│   └── gateway_computer_use.py  # 电脑控制
├── scripts/
│   ├── mimo_gateway.sh          # 启动脚本
│   └── claude_m1.sh             # Claude Code 启动
├── tests/                       # 测试
├── docs/                        # 文档
├── gateway.config.json          # 配置模板
└── .gateway_service.json        # 本地配置（不入 git）
```

---

## 文档

| 文档 | 说明 |
|------|------|
| [RUNNING_AND_TESTING.md](docs/RUNNING_AND_TESTING.md) | 部署与运行完整指南 |
| [full-gateway-tool-runtime-marketplace.md](docs/full-gateway-tool-runtime-marketplace.md) | 主方案架构 |
| [coding-agent-builtin-tools-implementation.md](docs/coding-agent-builtin-tools-implementation.md) | 内置工具实现 |
| [gateway-admin-ui-config.md](docs/gateway-admin-ui-config.md) | Admin UI 配置 |

---

## 许可证

[待定]
