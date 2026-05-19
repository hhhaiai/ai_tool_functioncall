# AI Tool FunctionCall Gateway 审查报告

## 总体结论

项目方向是对的，而且架构意识已经明显超过普通“OpenAI 兼容壳”。

你现在这套系统并不是“完全不支持 tools”，而是进入了第二阶段问题：

```text
协议兼容 ≠ 工具稳定可执行 ≠ agent runtime 完整
```

当前项目已经做到：

- OpenAI / Responses / Anthropic 三协议抽象
- native passthrough 思路
- provider capability probe
- fallback runtime
- MCP / HTTP action 规划
- Claude Code / Codex 兼容方向

这些方向都没问题。

真正的问题集中在：

1. capability verification 不够严格
2. tool forcing 缺失
3. orchestration 生命周期不完整
4. provider 行为差异没有真正隔离
5. 当前更像“协议网关”，还不是“完整 agent runtime”

---

# 核心问题（重点）

## 1. 当前 probe 误判率会很高

你现在的 probe 逻辑：

```text
只要返回 tool_calls 字段
≈ 判定支持 tools
```

这是不够的。

因为很多 provider：

- 会透传 tool_calls
- 会返回 finish_reason=tool_calls
- 但不会真正支持 roundtrip
- 或不会稳定 obey forced tool_choice

这是现在很多“兼容 OpenAI”网关的典型假支持。

你在 case 里已经遇到了：

```text
同一个 mimo 模型
fufu 会调用 tools
47.85 不调用
```

说明：

问题不在模型。

问题在 provider runtime 行为。

---

## 2. tool_choice=auto 被高估

你现在大量文档默认：

```json
"tool_choice": "auto"
```

但真实世界：

```text
auto = 模型自己决定
```

不是：

```text
一定会调用工具
```

所以：

你现在 gateway 的很多测试，实际上测试的是：

```text
模型主观意愿
```

而不是：

```text
provider tool runtime 能力
```

这是最大的逻辑问题之一。

---

## 3. 缺少“强制 tool call 验证”

你当前 probe 应该新增：

```json
"tool_choice": {
  "type": "function",
  "function": {
    "name": "echo_probe"
  }
}
```

只有 forced tool_choice 成功：

- finish_reason=tool_calls
- tool_calls/function_call/tool_use 返回正确
- arguments JSON 可解析

才算：

```text
native tools = true
```

否则：

```text
native tools = partial
```

这是整个项目当前最重要的修复点。

---

## 4. 你现在还缺真正的 tool lifecycle

当前设计更多是：

```text
LLM → tool_calls → 返回
```

但真正 production runtime 需要：

```text
LLM
 → tool selection
 → tool validation
 → permission gate
 → tool execution
 → timeout/retry
 → result normalization
 → result injection
 → final answer
```

你现在文档里提到了：

- MCP
- HTTP action
- builtin tools

但 execution lifecycle 还不完整。

尤其缺：

## 缺失项

### A. Tool registry

需要统一：

```text
name
schema
permission
executor
provider binding
```

### B. Tool sandbox

尤其 coding-agent：

```text
shell
filesystem
network
```

必须隔离。

### C. Tool execution trace

现在日志维度不够。

生产环境至少需要：

```text
request_id
session_id
tool_name
arguments
validated_arguments
execution_ms
provider
retry_count
error
```

---

## 5. Claude Code / Codex compatibility 会踩坑

这是你现在项目里最容易低估的部分。

Claude Code / Codex 真正依赖的是：

```text
协议 + runtime behavior + streaming semantics
```

不是：

```text
返回长得像 tool_calls
```

尤其：

### Claude

依赖：

```text
content blocks
stop_reason=tool_use
streaming block delta
```

### Codex

依赖：

```text
tool id consistency
parallel tool handling
tool result roundtrip
```

### OpenCode

依赖：

```text
稳定 schema
stream order
partial tool events
```

你现在文档已经意识到这一点，这是对的。

但还没真正进入 runtime consistency 阶段。

---

# 代码层面推测问题

虽然没完整跑代码，但从目录结构看，当前大概率存在：

## 1. provider adapter 与 orchestration 耦合

建议：

```text
provider adapter
只负责协议转换
```

不要负责：

```text
tool runtime
permission
retry
memory
```

否则后面会变成：

```text
if provider == anthropic:
if provider == openai:
if provider == kimi:
```

最后不可维护。

---

## 2. capability registry 应该独立

现在 capability 更像配置。

实际上应该：

```text
动态探测 + 缓存 + runtime override
```

否则：

provider 一升级。

你整个 compatibility matrix 全失真。

---

## 3. tool schema normalization 需要独立层

OpenAI:

```json
parameters
```

Anthropic:

```json
input_schema
```

Responses:

```json
function_call
```

建议统一内部协议：

```json
{
  "tool_name": "...",
  "input_schema": {},
  "transport": "openai|anthropic|responses"
}
```

不要在 runtime 内部混原始 provider schema。

---

# 真正建议的最终架构

建议最终拆成：

```text
Client Layer
  Claude Code / Codex / OpenCode

↓

Protocol Adapter Layer
  OpenAI / Anthropic / Responses

↓

Capability Layer
  provider probe
  feature registry
  streaming support

↓

Tool Runtime Layer
  validation
  permissions
  execution
  retries
  tracing

↓

Tool Provider Layer
  MCP
  HTTP actions
  builtin tools
  external function service

↓

Upstream LLM Layer
```

你现在：

```text
Adapter + Runtime
```

耦合偏高。

---

# 你当前最大的风险

不是 tools 不工作。

而是：

```text
“看起来支持”
```

这种状态最危险。

因为：

- Claude Code 会随机失效
- Codex 会中途丢 tool id
- streaming 会顺序错乱
- arguments 会半截 JSON
- multi-turn tool state 会漂移

这些问题：

demo 看不出来。

生产必炸。

---

# 下一步最应该做的（优先级）

## P0

- 强制 tool_choice probe
- tool roundtrip 验证
- streaming tool event 验证
- arguments strict JSON parse

---

## P1

- capability registry 独立化
- provider adapter 解耦
- tool runtime trace
- timeout / retry / permission

---

## P2

- Codex compatibility test suite
- Claude Code streaming compliance
- parallel tool call handling
- structured event replay

---

# 最后结论

这个项目：

```text
方向是对的。
```

而且已经明显超出“简单 API 转发器”。

但目前阶段：

```text
还是 Tool Protocol Gateway
```

还没真正进入：

```text
Production-grade Agent Runtime
```

差距主要在：

- runtime consistency
- execution lifecycle
- provider behavior isolation
- observability
- strict verification

而不是协议格式本身。

