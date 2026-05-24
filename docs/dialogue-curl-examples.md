# 不同对话 API 的 curl 调用形态

> 目标：先把常见对话接口梳理清楚，再决定如何在“不一定原生支持 tools/function call”的上游 API 之上补一层工具能力。

## 1. OpenAI Chat Completions：`POST /v1/chat/completions`

特点：
- 输入是 `messages: [{role, content}]`。
- 原生工具格式通常是 `tools: [{type:"function", function:{name, description, parameters}}]`。
- 如果模型决定调用工具，返回通常在 `choices[0].message.tool_calls`，`finish_reason` 可能是 `tool_calls`。

### 1.1 普通对话

```bash
curl "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'$MODEL'",
    "messages": [
      {"role": "system", "content": "你是一个简洁助手。"},
      {"role": "user", "content": "用一句话解释 function calling。"}
    ],
    "temperature": 0.2
  }'
```

### 1.2 原生 tools / function call 请求

```bash
curl "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'$MODEL'",
    "messages": [
      {"role": "user", "content": "计算 123 * 456 + 7"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "calculator",
          "description": "执行安全的数学四则运算",
          "parameters": {
            "type": "object",
            "properties": {
              "expression": {"type": "string", "description": "数学表达式"}
            },
            "required": ["expression"]
          }
        }
      }
    ],
    "tool_choice": "auto"
  }'
```

如果上游不支持 `tools`，通常会出现三类情况：
1. 直接 400：未知字段 `tools` / `tool_choice`。
2. 忽略 `tools`：模型正常回答，但不会产生 `tool_calls`。
3. 返回兼容字段但质量不稳定：字段存在，参数不是合法 JSON。

## 2. OpenAI Responses：`POST /v1/responses`

特点：
- 新项目通常更适合用 Responses 统一文本、工具、多模态等能力。
- 输入常用 `input`，可配 `instructions`。
- 原生工具也使用 `tools`，工具调用作为 response output item 出现。

### 2.1 普通对话

```bash
curl "$BASE_URL/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'$MODEL'",
    "instructions": "你是一个简洁助手。",
    "input": "用一句话解释 Responses API。"
  }'
```

### 2.2 原生 tools 请求

```bash
curl "$BASE_URL/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'$MODEL'",
    "input": "现在上海时间是多少？",
    "tools": [
      {
        "type": "function",
        "name": "get_current_time",
        "description": "获取指定时区当前时间",
        "parameters": {
          "type": "object",
          "properties": {
            "timezone": {"type": "string", "description": "IANA 时区，如 Asia/Shanghai"}
          },
          "required": ["timezone"]
        }
      }
    ],
    "tool_choice": "auto"
  }'
```

## 3. Anthropic Messages：`POST /v1/messages`

Claude Code / Anthropic SDK 如果配置：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8885/anthropic"
```

实际请求路径会是 `/anthropic/v1/messages`；Gateway 会兼容映射到内部
`/v1/messages`。直接 curl 时两种路径都可用：`$BASE_URL/v1/messages` 或
`$BASE_URL/anthropic/v1/messages`。

特点：
- Header 可使用 `x-api-key` 或 `Authorization: Bearer <key>`；Anthropic 客户端通常还会带 `anthropic-version`。
- 输入是 `messages`，但 `system` 通常是顶层字段，不放在 messages role 里。
- 工具定义常用 `{name, description, input_schema}`。
- 工具结果通过后续 user 消息中的 `tool_result` content block 传回。

### 3.1 普通对话

```bash
curl "$BASE_URL/v1/messages" \
  -H "x-api-key: $API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{
    "model": "'$MODEL'",
    "max_tokens": 1024,
    "system": "你是一个简洁助手。",
    "messages": [
      {"role": "user", "content": "用一句话解释 tool use。"}
    ]
  }'
```

### 3.2 原生 tools 请求

下面这种 `calc` / `expr` 形式已在 Gateway 中兼容到内置 `calculator` / `expression`，可直接用用户原始 curl 形态测试。

> `test-gateway-key` 是占位符，实际使用时替换为 `.gateway_service.json` 中的 `gateway.client_snippet_api_key` 或环境变量 `GATEWAY_DOWNSTREAM_KEY` 的值。

```bash
curl http://127.0.0.1:8885/anthropic/v1/messages \
  -H "Authorization: Bearer <your-gateway-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-pro",
    "messages": [
      {"role": "user", "content": "What is 2+2?"}
    ],
    "tools": [
      {
        "name": "calc",
        "description": "Evaluate math expression",
        "input_schema": {
          "type": "object",
          "properties": {"expr": {"type": "string"}},
          "required": ["expr"]
        }
      }
    ],
    "max_tokens": 100
  }'
```

通用 Anthropic tools 示例：

```bash
curl "$BASE_URL/v1/messages" \
  -H "x-api-key: $API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{
    "model": "'$MODEL'",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "计算 (8 + 9) * 7"}
    ],
    "tools": [
      {
        "name": "calculator",
        "description": "执行安全的数学四则运算",
        "input_schema": {
          "type": "object",
          "properties": {
            "expression": {"type": "string"}
          },
          "required": ["expression"]
        }
      }
    ]
  }'
```

## 4. 统一抽象

无论接口形态如何，都可以抽象成这几个步骤：

```text
用户输入 + 历史消息 + 工具定义
        ↓
模型判断：直接回答 or 需要工具
        ↓
如果需要工具：输出 {工具名, 参数 JSON}
        ↓
本地应用执行工具
        ↓
工具结果追加进上下文
        ↓
模型生成最终回答
```

因此，真正需要自己搭建的是 **工具编排层**，不是强依赖上游 API 原生 `tools` 字段。


## Admin: 拉取上游模型列表

Admin UI 的模型下拉按钮调用同一接口：

```bash
curl -fsS -u admin:admin "$BASE_URL/admin/upstream-models.json" | python3 -m json.tool
```

如果要按表单中的临时上游配置拉取，使用 POST body，避免把临时上游 `api_key` 放入 URL/query：

```bash
curl -fsS -u admin:admin "$BASE_URL/admin/upstream-models.json" \
  -H "content-type: application/x-www-form-urlencoded" \
  --data-urlencode "base_url=http://upstream.local:8885" \
  --data-urlencode "protocol=openai_chat" \
  --data-urlencode "path_models=/v1/models"
```
