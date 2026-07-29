# Tool / Function Call 格式兼容参考

> 最后校准：2026-07-29。当前实现覆盖 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 三类工具 schema、调用、结果与流式事件。

## 统一内部模型

Gateway 先把不同协议规范化为内部工具模型：

```text
GatewayTool: name + description + JSON schema + handler + risk + aliases
ToolCall:    id + name + arguments
ToolResult:  tool_call_id + name + content + success/failure metadata
```

主要代码：

- `src/gateway_protocol.py`：请求/响应协议转换；
- `src/gateway_tool_runtime.py`：工具调用提取、文本 fallback、结果回填和多轮编排；
- `src/gateway_streaming.py`：SSE 工具事件转换；
- `src/gateway_builtin_tools.py`：tool schema 和真实 handler registry。

## 输入 schema

| 协议 | 工具定义 |
|---|---|
| OpenAI Chat | `{"type":"function","function":{"name","description","parameters"}}` |
| OpenAI Responses | `{"type":"function","name","description","parameters"}` |
| Anthropic Messages | `{"name","description","input_schema"}` |

Gateway 会保留 JSON Schema 约束，并在 adapter/native/passthrough 选择前根据上游能力和当前路径转换。

## 模型返回的工具调用

| 协议 | 调用位置 | 参数 |
|---|---|---|
| OpenAI Chat | `choices[].message.tool_calls[]` | `function.arguments` JSON string |
| OpenAI Responses | `output[]` 中的 `function_call` item | `arguments` JSON string |
| Anthropic Messages | `content[]` 中的 `tool_use` block | `input` object |

内部 `_extract_tool_calls()` 会统一生成 `ToolCall`，并为缺失/冲突字段返回结构化失败，不把非法参数当作成功执行。

## 工具结果回填

| 协议 | 回填形态 |
|---|---|
| OpenAI Chat | `role: tool`, `tool_call_id`, `content` |
| OpenAI Responses | `function_call_output` item，使用对应 `call_id` |
| Anthropic Messages | user message 中的 `tool_result` content block，使用对应 `tool_use_id` |

Gateway-owned 工具执行后由 `_append_tool_results()` 回填；弱上游文本 adapter 使用有边界的文本结果格式。Downstream-owned 工具不会在 Gateway 伪造结果，而是先返回协议级调用，等待客户端回传真实结果。

## 结束原因

| 协议 | 工具阶段信号 |
|---|---|
| OpenAI Chat | `finish_reason: "tool_calls"` |
| OpenAI Responses | response output 包含 `function_call`；整体状态按协议封装 |
| Anthropic Messages | `stop_reason: "tool_use"` |

最终没有待执行工具时，转换层恢复普通完成语义。

## Streaming

### OpenAI Chat

Gateway 累积 `delta.tool_calls[].function.arguments`，在调用完整后交给 tool runtime；向下游发出兼容的 chunk 和 `[DONE]`。

### OpenAI Responses

Gateway 处理 response/output item 和 function-call argument 增量事件，并保持 `call_id` 关联。

### Anthropic Messages

工具输入使用 `content_block_start`、`content_block_delta` 和 `input_json_delta`；Gateway 会把非规范但可修复的上游工具输入规范化为 Anthropic 事件。

流式路径的首事件后不重放请求。chat-only rewrite 可以为了正确性缓冲决策轮，但 client disconnect 会取消上游。

## 文本 fallback

当上游不稳定支持原生 tools 时，Gateway 可以：

1. 将可调用工具以有界文本协议提供给上游；
2. 解析 `<function=...>`、JSON-like 或兼容工具标记；
3. 规范化工具名、alias 和 arguments；
4. 按 Gateway-owned / downstream-owned 分流；
5. 将真实结果回填并继续综合。

Fallback 是兼容手段，不改变工具所有权，也不允许把模型自然语言声明当作真实工具成功。

## Direct call 兼容

Direct tool endpoint 接受 canonical `tool`/`function` 名和 arguments，并兼容部分客户端的 `recipient_name`、`tool_uses` 和 alias 形态。所有形态最终进入同一 schema、ACL、权限、workspace 和 output-limit 检查。

## 关键边界

- `tool_choice` / forced-tool 的支持取决于路径和上游能力；未 live 验证时默认走 adapter；
- 调用 ID 必须在请求、结果和流式事件间保持关联；
- JSON arguments 解析失败是工具失败，不是空参数成功；
- 用户侧工具默认返回下游执行；
- HTTP/MCP/网络工具必须返回真实 connector 结果；
- 外部 Provider 协议兼容必须用 credentialed live E2E 单独验证。

## 验证

```bash
python3 -m pytest -q tests/test_gateway.py tests/test_claude_compat.py tests/test_gateway_true_streaming.py
python3 tests/integration/agent_planner_protocol_strict_smoke.py
./scripts/agent_planner_acceptance.sh --full
```
