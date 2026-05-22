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
   Gateway 提供 Web UI 和配置文件，用来管理上游 profile、下游 key、上游是否支持 tools、是否支持 vision、上下文窗口、工具权限、MCP、HTTP Actions 等。

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
| Admin UI / client config snippets | 已实现并有测试 | `src/gateway_admin.py` |

当前回归测试：

```bash
python3 -m unittest discover -s tests -v
# 131 tests OK
```

---

## 快速开始

```bash
# 1. 创建本地配置
cp gateway.config.json .gateway_service.json
vi .gateway_service.json  # 填 upstream.base_url / api_key / model 等

# 2. 启动
./scripts/mimo_gateway.sh start

# 3. 验证
curl http://127.0.0.1:8885/healthz
```

默认入口：

```text
API:       http://127.0.0.1:8885/v1/...
Admin UI:  http://127.0.0.1:8885/ui
管理员:    admin / admin（仅开发默认值）
```

---

## 下游接入

### Claude Code

```bash
./scripts/claude_m1.sh

# 或手动：
export ANTHROPIC_BASE_URL="http://127.0.0.1:8885"
export ANTHROPIC_API_KEY="your-gateway-key"
claude
```

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
| `/v1/models` | GET | 模型列表 |
| `/v1/chat/completions` | POST | OpenAI Chat |
| `/v1/responses` | POST | OpenAI Responses |
| `/v1/messages` | POST | Anthropic Messages |
| `/v1/messages/count_tokens` | POST | Anthropic token count 兼容 |
| `/v1/chat/completions/count_tokens` | POST | Chat token count 兼容 |
| `/v1/tools/call` | POST | 直接工具调用 |
| `/v1/functions/call` | POST | 直接工具调用兼容路径 |

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
- 写文件、Shell、GUI、网络类工具要通过配置显式授权。
- `gateway_app.py` 当前保留旧单体兼容导出层，新增实现应优先放入对应 `gateway_*` 模块。
