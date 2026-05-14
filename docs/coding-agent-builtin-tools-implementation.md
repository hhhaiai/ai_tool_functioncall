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

实现文件：`src/toolcall_gateway.py`

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
| `WebFetch`, `fetch_url` | ready | read_network | HTTP(S) fetch |
| `TodoWrite`, `todo_write` | ready | state | 接收 todo payload |
| `update_plan` | ready | state | 接收 plan payload |
| `Write`, `write_file` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1` |
| `Edit`, `edit_file` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1` |
| `MultiEdit` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1` |
| `apply_patch` | gated | write_local | 需 `GATEWAY_ALLOW_WRITE_TOOLS=1`，调用 `apply_patch` CLI |
| `Bash`, `exec_command`, `shell_command`, `exec_shell` | gated | execute_code | 需 `GATEWAY_ALLOW_SHELL_TOOLS=1` |

### 2.3 已注册但需要 connector/runtime 的工具

这些是四个参考项目中会出现的工具名，当前 Gateway 不会崩溃，会返回协议级 `connector_required` tool result，并写入失败日志，等待 MCP/OpenAPI/plugin marketplace 接入：

```text
WebSearch / web_search
Task / Agent
spawn_agent / wait_agent / close_agent
request_user_input / AskUserQuestion
Skill
NotebookEdit
ListMcpResourcesTool / ReadMcpResourceTool
list_mcp_resources / list_mcp_resource_templates / read_mcp_resource
view_image
write_stdin
exec_shell_wait / exec_shell_interact / exec_wait / exec_interact
```

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

1. MCP connector：实现 `tools/list`、`tools/call`，补齐 MCP resource 和第三方工具市场。
2. `write_stdin` / `exec_shell_wait` / `exec_shell_interact`：需要持久 shell session runtime。
3. `Task/Agent/spawn_agent`：需要子代理 runtime。
4. `WebSearch`：需要 web search provider 或 MCP/search connector。
5. `view_image`：需要文件读取 + vision model connector。
6. 完善 streaming tool events；当前只保证 non-streaming orchestration。
