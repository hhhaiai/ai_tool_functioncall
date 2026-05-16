# Coding Agent 内置工具兼容实现记录

## 1. 参考来源

本实现参考以下项目的真实 tool/function-call 形态：

- Claude Code：`/Users/sanbo/code/claude-code-source/src/tools.ts:193-250` 注册基础工具，如 `Bash`、`Read`、`Edit`、`Write`、`Glob`、`Grep`、`WebFetch`、`WebSearch`、`TodoWrite`、`Agent/Task`、MCP resource tools。
- Claude Code：`/Users/sanbo/code/claude-code-source/src/utils/api.ts:119-178` 把工具转为 Anthropic tool schema；`src/services/api/claude.ts:1699-1713` 发送 `tools/tool_choice`。
- Codex：`/Users/sanbo/code/codex/codex-rs/core/src/tools/spec_plan.rs:247-434` 注册 `exec_command`、`write_stdin`、`apply_patch`、`view_image`、`update_plan`、MCP resources、multi-agent tools 等。
- Codex：`/Users/sanbo/code/codex/codex-rs/core/src/client.rs:709-752` 通过 Responses API 发送 `tools`、`tool_choice=auto`、`parallel_tool_calls`。
- DeepSeek-TUI：`/Users/sanbo/code/DeepSeek-TUI/crates/tui/src/tools/registry.rs:394-870` 注册 file/search/shell/web/patch/todo/plan/MCP/subagent 工具。
- DeepSeek-TUI：`/Users/sanbo/code/DeepSeek-TUI/crates/tui/src/client/chat.rs:1116-1138,1240-1266,1713-1749` 负责 `tool_calls` 与 `role:tool` 的序列化/解析。
- claude-code-tamagotchi：`/Users/sanbo/code/claude-code-tamagotchi/src/workers/analyze-transcript.ts:235-296` 明确常见 Claude Code transcript 工具：`Read`、`Edit`、`Write`、`MultiEdit`、`Bash`、`Glob`、`Grep`、`LS`、`WebFetch`、`WebSearch`、`Task`、`TodoWrite`。

## 2. 当前 Gateway 已落地能力

实现文件：

- `src/toolcall_gateway.py`：兼容入口，保留旧导入路径。
- `src/gateway_app.py`：HTTP 协议、配置/Admin UI、Claude/OpenAI/Anthropic 适配、MCP、context fan-out。
- `src/gateway_builtin_tools.py`：内置工具实现和别名注册。

### 2.1 Runtime 骨架

- `GatewayTool` / `ToolCall` / `ToolResult` 内部模型：`src/toolcall_gateway.py:64-88`
- 三协议 tool call 提取：`src/toolcall_gateway.py:655-710`
- 三协议 tool result 回填：`src/toolcall_gateway.py:720-775`
- 多轮 tool orchestration loop：`src/toolcall_gateway.py:849-867`
- 失败记录 JSONL：`src/toolcall_gateway.py:778-846`
- 默认 `GATEWAY_TOOL_MODE=orchestrate`，可切回 `passthrough`。

### 2.2 已内置实现的工具

| 工具名/别名 | 状态 | 风险 | 说明 |
|---|---|---|---|
| `echo_probe`, `gateway__echo_probe` | ready | pure | 原生 tool probe |
| `calculator`, `gateway__calculator` | ready | pure | 安全 AST 算术 |
| `get_current_time`, `current_time` | ready | pure | 当前时间 |
| `Read`, `read_file`, `FileReadTool` | ready | read_local | workspace 内读文件 |
| `LS`, `list_dir` | ready | read_local | workspace 内列目录 |
| `Glob`, `glob_files`, `find_files` | ready | read_local | workspace 内 glob |
| `Grep`, `grep_files`, `file_search` | ready | read_local | workspace 内正则搜索 |
| `WebFetch`, `fetch_url`, `web_fetch`, `fetch` | ready | read_network | HTTP(S) fetch |
| `WebSearch`, `web_search`, `web_search_preview` | ready | read_network | DuckDuckGo HTML search，失败时返回真实连接/解析错误，不伪造结果 |
| `TodoWrite`, `todo_write` | ready | state | 接收 todo payload |
| `update_plan` | ready | state | 接收 plan payload |
| `ExitPlanMode`, `EnterPlanMode` | ready | state | 接收/回显 plan-mode payload |
| `view_image` | ready | read_local | 返回本地图片 metadata，可选 base64 前缀 |
| `list_mcp_resources` | ready | mcp | 调用已配置 MCP server 的 `resources/list` |
| `list_mcp_resource_templates` | ready | mcp | 调用已配置 MCP server 的 `resources/templates/list` |
| `read_mcp_resource`, `mcp_read_resource` | ready | mcp | 调用已配置 MCP server 的 `resources/read` |
| `mcp_get_prompt` | ready | mcp | 调用已配置 MCP server 的 `prompts/get` |
| `multi_tool_use.parallel`, `parallel` | ready | orchestration | 并发执行多个 Gateway tool call，禁止递归 parallel |
| `Agent`, `Task`, `spawn_agent`, `subagent` | ready | ai_agent | 调上游模型执行子任务；大 prompt 会走分片分析+汇总 |
| `Skill`, `list_skills` | ready | ai_agent | 读取本地 `SKILL.md` 并组合 Agent 执行，或列出 skills |
| `Tree`, `tree` | ready | read_local | workspace 内树形目录 |
| `JsonQuery`, `jq` | ready | read_local | JSON 文件点路径查询 |
| `PythonSymbols`, `python_symbols` | ready | read_local | Python AST 符号提取 |
| `Write`, `write_file` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1` |
| `Edit`, `edit_file` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1` |
| `MultiEdit` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1` |
| `RegexEdit` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1` |
| `NotebookEdit`, `notebook_edit` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1`，编辑 `.ipynb` cells |
| `apply_patch` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1`，调用 `apply_patch` CLI |
| `Bash`, `exec_command`, `shell_command`, `exec_shell`, `local_shell`, `user_shell` | gated | execute_code | 需 `GATEWAY_ALLOW_SHELL_TOOLS=1` |
| `exec_shell_start`, `write_stdin`, `exec_wait`, `exec_kill` | gated | execute_code | 持久 shell session，需 `GATEWAY_ALLOW_SHELL_TOOLS=1` |

### 2.3 已注册但需要 connector/runtime 的工具

这些是四个参考项目中会出现、但不能在纯 HTTP gateway 内安全/完整本地实现的工具名。当前 Gateway 不会崩溃，会返回协议级 `connector_required` tool result，并写入失败日志，等待 MCP/OpenAPI/plugin marketplace 接入：

```text
request_user_input / AskUserQuestion
ListMcpResourcesTool / ReadMcpResourceTool
computer_use / computer_use_preview
WebBrowser / click / type_text / press_key / scroll
```

**注意：** `code_interpreter` 已实现为真实工具（第382行 `_tool_code_interpreter`），使用 `subprocess.run` 执行 Python 代码。默认禁用，需设置 `GATEWAY_ALLOW_SHELL_TOOLS=1` 启用。

补全策略：优先通过 MCP 市场安装对应 server（浏览器、GitHub、数据库、文件系统、搜索、代码执行等），其次通过 Admin UI 的 HTTP Action 把已有服务注册为工具。Gateway 会自动暴露 `mcp__server__tool` 和 `mcp_server_tool` 两套名称。

## 3. 安全默认值

默认安全策略：

```bash
GATEWAY_TOOL_MODE=orchestrate
GATEWAY_EXPOSE_BUILTIN_TOOLS=1
GATEWAY_MAX_TOOL_ROUNDS=5
GATEWAY_ALLOW_WRITE_TOOLS=0
GATEWAY_ALLOW_SHELL_TOOLS=0
GATEWAY_WORKSPACE_ROOT=$PWD
```

写文件、编辑、patch、shell 都已经实现，但默认禁用；启用前必须指定 workspace root 并确认沙箱策略。

## 4. 后续必做

1. MCP connector 已支持 `tools/list`、`tools/call`、resource/prompt helpers；下一步是接入可浏览/安装的 MCP 市场和认证配置。
2. `request_user_input` 仍需要交互式客户端配合；Gateway 会记录未支持调用。
3. `computer_use` / 浏览器 UI：需要桌面权限和持久 UI runtime，建议走 MCP/插件。
4. 完善 streaming tool events；当前 orchestrate 模式会内部非流式执行工具并输出最终 SSE，passthrough 模式透传上游 SSE。
5. 商业化稳定性继续增强：多租户 key、配额、审计、MCP 市场安装器、provider 能力自动探测。
