# 自建 tools / function call 支持方案

## 结论

如果上游 API 只支持普通对话，不稳定或不支持 `tools/function_call`，推荐加一层本地 **Tool Shim Gateway**：

```text
你的客户端
  └─按 OpenAI/Anthropic tools 格式请求→ 本地网关
        ├─把 tools 转成系统提示/JSON 协议
        ├─调用普通对话上游 API
        ├─解析模型输出的工具调用 JSON
        ├─执行本地工具函数
        └─再次调用上游 API 生成最终回答
```

这样客户端统一调用本地网关，后面可以接：
- `/v1/chat/completions`
- `/v1/responses`
- `/v1/messages`
- 其他只要“能对话”的模型 API

## 为什么不只靠上游原生 tools

很多 OpenAI-compatible API 会声称兼容，但实际差异很大：

| 能力 | 常见问题 |
|---|---|
| `tools` 字段 | 400、忽略、或只部分模型支持 |
| `tool_choice` | 不识别或不遵守 |
| 参数 JSON | 字符串非 JSON、字段缺失、幻觉工具名 |
| 多轮工具 | 不支持 tool result 回传格式 |
| Responses / Messages | 同名能力的 JSON 结构完全不同 |

自建 shim 的好处是把这些差异收敛到一处。

## Shim 内部协议

网关注入系统提示，要求模型只输出两种 JSON：

### 需要工具

```json
{
  "type": "tool_call",
  "tool_calls": [
    {
      "name": "calculator",
      "arguments": {"expression": "123 * 456 + 7"}
    }
  ]
}
```

### 最终回答

```json
{
  "type": "final",
  "content": "123 * 456 + 7 = 56095。"
}
```

如果模型没有遵守 JSON 协议，网关会把原文当作最终回答返回。这保证“普通对话”不被中断，但工具调用可靠性取决于模型遵守指令的能力。

## 运行网关

```bash
export UPSTREAM_BASE_URL="https://api.example.com"
export UPSTREAM_API_KEY="sk-xxx"
export UPSTREAM_MODEL="model-name"
export UPSTREAM_KIND="chat"      # chat | responses | messages

python3 src/toolcall_gateway.py --host 127.0.0.1 --port 8787
```

## 通过本地网关调用 Chat Completions + tools

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer local-anything' \
  -H 'Content-Type: application/json' \
  -d @examples/chat-with-tool.json
```

## 通过本地网关调用 Responses + tools

```bash
curl http://127.0.0.1:8787/v1/responses \
  -H 'Authorization: Bearer local-anything' \
  -H 'Content-Type: application/json' \
  -d @examples/responses-with-tool.json
```

## 通过本地网关调用 Messages + tools

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'x-api-key: local-anything' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d @examples/messages-with-tool.json
```

## 生产化边界

当前网关是最小可跑版本，适合作为原型。生产化需要继续补：

1. 工具参数 JSON Schema 校验。
2. 工具白名单、鉴权、限流、超时和审计日志。
3. stream/SSE 透传和工具事件流。
4. 多工具并发、重试、幂等 key。
5. 长上下文裁剪和对话状态存储。
6. 防 prompt injection：工具结果要作为数据处理，不允许覆盖系统指令。
7. 按上游能力自动选择 native tools 或 shim tools。

## 推荐落地顺序

1. 先用当前 shim 打通普通 tools 流程。
2. 把业务工具接入 `TOOL_REGISTRY_MODULE`。
3. 给每个工具补 schema 校验和权限边界。
4. 再做 native/shim 自动探测：上游支持原生 tools 时直通，否则走 shim。
