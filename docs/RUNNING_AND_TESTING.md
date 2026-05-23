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
    "base_url": "https://api.openai.com",
    "api_key": "sk-your-api-key",
    "model": "gpt-4o"
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
    "base_url": "https://api.openai.com",
    "api_key": "sk-xxx",
    "model": "gpt-4o",
    "protocol": "openai_chat",
    "tools_enabled": "auto",
    "timeout_seconds": 60,
    "max_input_tokens": 128000,
    "max_output_tokens": 8192,
    "max_concurrency": 32
  },
  "gateway": {
    "workspace_root": "./workspace",
    "tool_mode": "orchestrate",
    "allow_write_tools": false,
    "allow_shell_tools": false,
    "max_tool_rounds": 10,
    "tool_execution_timeout_seconds": 60,
    "max_concurrent_requests": 32,
    "request_logging": true,
    "logging_backend": "sqlite",
    "public_base_url": "http://127.0.0.1:8885",
    "client_snippet_api_key": "your-gateway-key",
    "downstream_model_alias": "",
    "local_planner_enabled": true,
    "local_planner_max_files": 24
  },
  "context": {
    "enabled": true,
    "max_input_tokens": 24000,
    "fanout_enabled": true,
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
| `upstream.base_url` | - | 上游 LLM API 地址 |
| `upstream.api_key` | - | 上游 API Key |
| `upstream.model` | - | 默认模型名称 |
| `upstream.protocol` | openai_chat | 上游协议类型 |
| `gateway.workspace_root` | `./workspace`（模板）/ 当前目录（无配置时） | 工具读写的根目录 |
| `gateway.tool_mode` | orchestrate | 工具模式：orchestrate / passthrough |
| `gateway.allow_write_tools` | false | 是否允许文件写入 |
| `gateway.allow_shell_tools` | false | 是否允许 Shell 执行 |
| `gateway.client_snippet_api_key` | - | 客户端连接 Gateway 的 API Key；保存配置时会自动同步为可认证的 downstream key |
| `gateway.max_request_body_bytes` | 67108864 | HTTP POST 请求体读取前上限；超限返回 413，避免大请求先占用内存 |
| `context.max_input_tokens` | 24000 | 超过此值触发上下文压缩 |

### 3.4 环境变量对照表

| 环境变量 | 配置路径 | 说明 |
|----------|----------|------|
| `UPSTREAM_BASE_URL` | upstream.base_url | 上游 API 地址 |
| `UPSTREAM_API_KEY` | upstream.api_key | 上游 API Key |
| `UPSTREAM_MODEL` | upstream.model | 默认模型 |
| `GATEWAY_UPSTREAM_PROTOCOL` | upstream.protocol | 上游协议类型，优先于 legacy `UPSTREAM_PROTOCOL` |
| `UPSTREAM_PROTOCOL` | upstream.protocol | 兼容旧环境变量，未设置 `GATEWAY_UPSTREAM_PROTOCOL` 时生效 |
| `GATEWAY_DOWNSTREAM_KEY` | downstream key + gateway.client_snippet_api_key | 下游 API Key；环境变量会同时用于认证和客户端片段 |
| `GATEWAY_ADMIN_PASSWORD` | admin.password | 管理员密码 |
| `GATEWAY_WORKSPACE_ROOT` | gateway.workspace_root | 工作目录 |
| `GATEWAY_PORT` | - | 监听端口（默认 8885） |
| `GATEWAY_HOST` | - | 监听地址（默认 0.0.0.0） |
| `GATEWAY_SQLITE_LOG_PATH` | gateway.sqlite_log_path | SQLite 请求/工具/记忆日志路径 |
| `GATEWAY_MAX_REQUEST_BODY_BYTES` | gateway.max_request_body_bytes | POST 请求体读取前字节上限，默认 64MB，超限返回 413 |

配置文件存在但 JSON 损坏或根节点不是对象时，Gateway 会 fail closed：Admin/API 请求返回结构化 500 `invalid gateway config`，不会静默回退到默认 `admin/admin` 或无下游鉴权。修复方式是恢复有效 `.gateway_service.json`，而不是依赖代码默认值。

HTTP Action 执行遵循真实 executor 契约：`GET` / `DELETE` 使用 query，`POST` / `PUT` / `PATCH` 使用 JSON body，`headers` 可通过 `${ENV_NAME}` 注入环境变量，`max_bytes` 默认限制响应体为 1MB；HTTP/URL/响应超限错误会记录为 tool failure，且默认不重试以避免外部副作用重复执行。

Gateway 会在读取前限制 HTTP POST 请求体大小：`gateway.max_request_body_bytes` / `GATEWAY_MAX_REQUEST_BODY_BYTES` 默认 64MB，超限返回结构化 413，避免 API 请求或 Admin form 在进入上下文压缩/业务校验前占用过多内存。配置了 downstream key 时，受保护 `/v1/*` 和 direct-tool POST 会先校验 key，再读取/解析 JSON body；未授权 malformed/oversized body 仍返回 401。

请求/响应日志和 Admin 配置展示会递归遮盖常见敏感字段，包括 `Authorization`、`X-API-Key`、`Cookie`、token、secret、password、`key_hash` 等；`must_change_password` 等非敏感状态字段会保留原值。

Admin 写操作会校验浏览器 `Origin` / `Referer`：跨源请求返回 403，畸形来源 fail closed；同源请求和无来源头的 CLI/脚本请求保持可用。反向代理部署时请正确传递 `Host` / `X-Forwarded-Host` / `X-Forwarded-Proto`，或配置 `gateway.public_base_url`。

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
./scripts/mimo_gateway.sh restart

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
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# 查看可用模型
curl http://127.0.0.1:8885/v1/models \
  -H "Authorization: Bearer your-gateway-api-key"
```

### 5.4 运行测试套件

```bash
# 运行全部测试（当前 148 个）
python3 -m unittest discover -s tests -v

# 运行集成测试
python3 tests/integration/smoke_gateway_tools.py
```

---

## 6. 客户端接入

### 6.1 Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8885
export ANTHROPIC_API_KEY=your-gateway-api-key
claude
```

### 6.2 OpenCode

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8885/v1
export OPENAI_API_KEY=your-gateway-api-key
opencode
```

### 6.3 Codex CLI

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8885/v1
export OPENAI_API_KEY=your-gateway-api-key
codex
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
    "tools_enabled": "auto"
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
| `/v1/models` | GET | 模型列表 |
| `/v1/chat/completions` | POST | OpenAI Chat 接口 |
| `/v1/responses` | POST | OpenAI Responses 接口 |
| `/v1/messages` | POST | Anthropic Messages 接口 |
| `/v1/tools/call` | POST | 直接工具调用 |

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

**最后更新**: 2026-05-19
