# Coding Agent 内置工具参考

> 最后校准：2026-07-29。工具数量和名称以 `src/gateway_builtin_tools.py:BUILTIN_TOOLS` 与 `GET /capabilities` 为准。

## 当前规模

```text
唯一 canonical 工具：67
含别名 registry key：178
```

`GatewayTool.name` 是 canonical 名称；`BUILTIN_TOOLS` 同时保存 Claude Code、Codex、OpenAI、Anthropic 和常见 MCP/agent 命名的兼容别名。

## 调用链

```text
下游 tools / function schema
  → gateway_protocol.py 规范化协议
  → gateway_tool_runtime.py 提取 ToolCall
  → 工具所有权判断
      ├─ Gateway-owned → gateway_builtin_tools.py / MCP / HTTP Action 真执行
      └─ downstream-owned → 返回下游原生 tool request
  → ToolResult 回填
  → 下一轮上游综合或直接响应
```

主要实现点：

- `src/gateway_builtin_tools.py`：`GatewayTool`、schema、canonical 实现和 alias registry；
- `src/gateway_tool_runtime.py`：调用提取、参数规范化、所有权判断、多轮编排和 direct call；
- `src/gateway_protocol.py`：三协议工具 schema、tool call 和 tool result 转换；
- `src/gateway_mcp.py`：MCP resources/tools/prompts；
- `src/gateway_http_actions.py`：配置型 HTTP 工具；
- `src/gateway_sandbox.py` / `src/gateway_process_ops.py`：进程、workspace、timeout 和取消边界。

## 工具所有权

### Gateway-owned

这些能力可以在 Gateway 服务侧真实执行：

| 类别 | 代表工具 |
|---|---|
| 纯函数 | `echo_probe`, `calculator`, `get_current_time` |
| Gateway memory/state | `Memory`, `SaveMemory`, `RecallMemory`, `TodoWrite`, `update_plan` |
| 网络 | `WebFetch`, `WebSearch`, `WebBrowser`，受私网/SSRF/大小/超时边界约束 |
| MCP | `list_mcp_resources`, `read_mcp_resource`, `mcp_list_tools`, `mcp_call_tool` |
| HTTP Actions | Admin 配置的 action；真实 HTTP 响应作为工具结果 |
| 编排 | `multi_tool_use.parallel`，禁止递归 parallel |

Gateway-owned 不代表永远成功：依赖、权限、网络、Provider 或 MCP server 不可用时会返回结构化失败并写入失败记录，不生成 placeholder 成功。

### Downstream-owned

这些工具默认属于用户机器：

| 类别 | 代表工具 |
|---|---|
| 读取项目 | `Read`, `LS`, `Glob`, `Grep`, `Tree`, `PythonSymbols` |
| 修改项目 | `Write`, `Edit`, `MultiEdit`, `RegexEdit`, `NotebookEdit`, `apply_patch` |
| 运行命令 | `Bash`, `exec_command`, `exec_shell_start`, `write_stdin`, `code_interpreter` |
| 用户交互/插件 | `Skill`, `request_user_input`, GUI/computer-use, local agent/subagent |

默认配置：

```json
{
  "gateway": {
    "execute_user_side_tools_in_gateway": false
  }
}
```

此时 Gateway 返回对应协议的 `tool_use`、`tool_calls` 或 `function_call`，由 Claude Code/Codex 在当前用户项目执行，再把结果送回 Gateway。

本地代理部署只有同时满足以下条件，才允许服务机执行用户侧工具：

1. `gateway.execute_user_side_tools_in_gateway=true` 或 `GATEWAY_EXECUTE_USER_SIDE_TOOLS=1`；
2. 写操作还需要 `gateway.allow_write_tools=true` / `GATEWAY_ALLOW_WRITE_TOOLS=1`；
3. shell 操作还需要 `gateway.allow_shell_tools=true` / `GATEWAY_ALLOW_SHELL_TOOLS=1`；
4. 请求 workspace 必须通过项目根解析和沙箱检查。

`delegate_tools_to_downstream=false` 不等于授权服务端执行。

## 项目根与 Skills

工具 workspace 按请求解析：显式 `workspace_root` / `gateway_workspace` 优先，其次解析 Claude Code、Codex 和 metadata 中的项目目录信号；缺失时进入匿名隔离空间，不回退 Gateway 服务 checkout。

Skills 搜索顺序以当前请求项目为核心：

1. 项目级 `.codex/skills`、`.claude/skills`、`.opencode/skills`、`.agents/skills`、`skills/`；
2. 项目插件 manifest 声明且仍位于项目根内的 skills；
3. Admin 服务级 catalog；
4. 用户全局和 `GATEWAY_SKILLS_DIRS`。

项目级 Skill 默认仍是 downstream-owned；Admin 可以展示 Gateway 可见的 catalog。

## Direct call

Gateway-owned 工具可通过以下兼容路径直接调用：

```text
POST /v1/tools/call
POST /v1/functions/call
POST /tools/call
POST /anthropic/v1/tools/call
POST /anthropic/v1/functions/call
```

示例：

```bash
curl -fsS "$BASE_URL/v1/tools/call" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool":"calculator","arguments":{"expression":"6*7"}}'
```

Direct call 仍执行下游 key、ACL、输入 schema、工具权限、workspace 和输出上限检查。

## 失败和可观测性

- 未知工具或非法参数返回客户端错误；
- connector/依赖缺失返回结构化工具失败；
- timeout、cancel、输出超限和进程退出会进入失败记录；
- `/admin/tools.json` 展示当前 registry；
- `/admin/agent-runtime-events.json` 与 `/admin/agent-runtime-audit.json` 展示编排边界；
- `gateway_logging.py` 记录工具统计与失败，敏感字段会遮盖。

## 验证

```bash
python3 -m pytest -q tests/test_gateway.py tests/test_tool_parallel.py tests/test_tool_runtime_stability.py
./scripts/agent_planner_acceptance.sh --full
```

完整发布仍以 `./scripts/ci_gate.sh` 为准。
