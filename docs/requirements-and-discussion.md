# Tool Call Gateway 需求与讨论记录

**日期：** 2026-05-16
**状态：** 进行中

---

## 1. 项目定位

Gateway 是一个**真实的工具执行 runtime**，不是 prompt faker。

**核心原则（来自 README）：**
- 可以 adapter，但不能 fake
- 可以 fallback，但 fallback 必须连接真实工具 runtime 或真实 native-tools provider
- 不允许把 tools 写进 prompt，再把模型文本 JSON 伪装成 `tool_calls` / `tool_use`

**Gateway 的职责：**
1. 接收客户端标准 tools/function-call 请求
2. 转发给上游 AI
3. 接收上游返回的 tool_calls（协议级或文本级）
4. 在 Gateway 内部执行真实工具
5. 把 tool/function result 合并回上游 AI
6. 循环直到最终回答
7. 返回给客户端

---

## 2. 当前问题

### 2.1 tool_calls 未被执行

**现象：** Gateway 收到上游返回的 tool_calls 后，没有执行它们，直接透传给客户端。

**用户反馈：**
```
分析这套代码

⏺ Let me explore the codebase.
  <function=Glob>
  <parameter=pattern>/*.py

  <function=Glob>
  <parameter=pattern>/*.md

✻ Cogitated for 48s

❯ 分析 @src/ 中所有代码，逐个类分析
  ⎿  Listed directory src/

⏺ Sorry, the text you sent is too long! I suggest you simplify the content
  appropriately or send it in parts. Thank you for your understanding.
```

**分析：** 上游 API 不支持原生 tool_calls 协议。模型把工具调用写在文本中（如 `<function=Glob>`），Gateway 的 `_native_tool_signal()` 无法识别，直接透传。

### 2.2 上游 API 不支持 tools

**用户明确说明：** 上游现在什么都不支持，希望 Gateway 执行工具/tool_calls。

**这意味着：**
- 上游 API 不支持原生 tool_calls 协议
- 模型在文本中生成工具调用语法
- Gateway 需要解析文本，执行真实工具，再把结果回传给上游

---

## 3. 需求列表

### 3.1 核心需求

| # | 需求 | 优先级 | 状态 |
|---|------|--------|------|
| 1 | **文本级工具调用识别与执行** - 解析模型文本输出中的工具调用语法，执行真实工具 | P0 | **已实现** |
| 2 | **code_interpreter 本地沙箱** - 在 Gateway 本地执行 Python 代码 | P1 | **已实现** |
| 3 | **智能执行流水线** - 高风险工具走5步流程（语义分析→执行→检查→反思→结论） | P1 | 待实现 |
| 4 | **无限上下文** - 支持超长输入的分片处理和上下文压缩 | P1 | 部分实现 |
| 5 | **支持稳定真实 tool calls** - 覆盖项目识别、文件读写改、Shell/coding、网络查询、并行工具、SQLite 记录 | P0 | **已实现并有 smoke 测试** |
| 6 | **兼容性强** - 支持多种上游 API 和客户端格式 | P1 | 部分实现 |
| 7 | **稳定运行** - 多并发支持，生产级稳定性 | P1 | 待加固 |


### 3.4 当前收敛范围（2026-05-16）

本轮优先级是稳定真实 tools + 三协议下游兼容 + 简洁启动：

- 默认上游：由环境变量或本地 `.gateway_service.json` 配置；公开文档不包含测试地址和真实 key。
- 上游可只有 OpenAI `/v1/chat/completions`；Gateway 对下游仍提供 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`，内部统一转到上游 chat completions。
- 默认认为上游不支持 native tools/function calls：Gateway 不把大 `tools` schema 透传给上游，而是本地真实执行 tools。
- Gateway 本地真实执行 tools：项目识别、文件读取/写入/编辑、Bash/coding、WebFetch/WebSearch、并行工具。
- 高频日志只写 SQLite WAL：`gateway_log.sqlite3`；JSONL/JSON 仅作为历史导入/读取，不作为默认写入后端。
- Web UI 当前支持多上游 API profile、上游能力勾选、下游多 key + 协议限制。
- 脚本只保留两个：`scripts/mimo_gateway.sh` 和 `scripts/claude_m1.sh`。

验证入口：

```bash
./scripts/mimo_gateway.sh verify
./tests/integration/tool_acceptance.py
./tests/integration/smoke_gateway_tools.py
python3 -m unittest discover -s tests -v
```

### 3.2 上游能力配置

**用户要求：** 在配置页面可以设置上游 API 是否支持 tools。

**需要支持的上游能力配置项：**
- `supports_tools` - 是否支持原生 tool_calls
- `supports_vision` - 是否支持识图
- `supports_network` - 是否支持网络
- `supports_streaming` - 是否支持流式
- `supports_hosted_code_interpreter` - 是否支持 hosted code_interpreter
- `supports_hosted_web_search` - 是否支持 hosted web_search
- `supports_hosted_file_search` - 是否支持 hosted file_search

### 3.3 工具执行策略

**用户要求：**
- 如果上游支持工具，使用上游的工具实现
- 本地也必须有实现的方案作为兜底
- 高质量实现，不是简单调用
- 包含语义分析→调用→检查→反思→调整→最终结论

**风险分流策略（用户确认）：**
- 低风险工具（calculator, Read, Glob 等）：直接执行
- 高风险工具（Bash, code_interpreter 等）：走智能执行流水线

---

## 4. 技术方案

### 4.1 文本级工具调用解析器

**已实现位置：** `src/gateway_app.py` 第2174行 `_parse_text_tool_calls()`

**支持的格式：**

**格式1 - XML 风格（Claude Code-like）：**
```
<function=Glob>
<parameter=pattern>**/*.py

## 2026-05-16 更新：SQLite 记忆与更多真实 tools

- Gateway 现在默认启用 SQLite 会话记忆，按 session/workspace 隔离，支持 compact summary + keyword recall。
- 无限上下文不再尝试塞满原文，而是：记忆摘要、局部 planner 证据、超大上下文 fanout 分片、综合审查。
- 新增/增强真实兼容工具：LSP、WebBrowser、file_search_call、web_search_call、web_search_preview_2025_03_11、spawn_agent/send_input/wait_agent/close_agent/resume_agent、request_user_input、TeamCreate/SendMessage/TeamDelete、ListMcpResourcesTool/ReadMcpResourceTool。
- 仍需外部 connector/MCP 的工具会继续记录到 SQLite failures，作为后续 marketplace 增强入口。
