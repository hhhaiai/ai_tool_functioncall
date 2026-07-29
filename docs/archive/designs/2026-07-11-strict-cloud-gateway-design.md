# Strict Cloud Gateway Design

## Goal

让独立部署的 Gateway 为完全不支持 `tool_calls` / `function_call` 的普通聊天上游补齐下游原生工具协议，并通过压缩、记忆、fan-out 与综合为有限上下文上游提供类无限上下文能力，供 Codex 与 Claude Code 使用。

## Architecture

Gateway 保持严格的服务端/用户端工具归属边界。HTTP Actions、MCP、WebFetch、WebSearch、calculator、Memory 等 Gateway-owned 工具由服务端执行；Read、Write、Edit、Glob、Grep、Bash、Git、Skill、GUI、computer-use 等依赖用户机器或用户项目的工具转换成 OpenAI Chat `tool_calls`、OpenAI Responses `function_call` 或 Anthropic Messages `tool_use`，由 Codex/Claude Code 执行并回传结果。

弱上游默认使用 `tools_enabled=adapter`，且声明 `supports_tools=false`、`supports_function_calls=false`。Gateway 可以向弱上游注入受控文本工具协议、解析弱上游的文本调用，但对下游必须输出真实协议对象，不能把可见文本伪装成工具能力。

长上下文不是物理无限 token。Gateway 在上游限制内通过 token 估算、旧历史压缩、近期消息保留、SQLite 会话记忆、超长输入 fan-out、结果综合与可选质量复核维持连续使用效果，并按 tenant、workspace、session 隔离状态。

## Required Changes

1. 正式 CLI smoke 不再通过 `/v1/tools/call` 要求 Gateway 执行用户侧 `Skill`、`Read`。
2. direct-tool 部分只验证 Gateway-owned 工具和服务端记忆隔离。
3. Anthropic/Responses streaming 必须验证返回下游原生工具请求，而不是接受 Gateway 本地读取后的文本作为替代。
4. Claude Code 与 Codex CLI 必须在临时下游项目中执行工具并返回项目 marker。
5. 实际服务配置启用 `context.enabled`、`fanout_enabled`、`quality_review_enabled`、`memory_enabled`。
6. 实际服务保持 `execute_user_side_tools_in_gateway=false`。

## Acceptance

- focused protocol/context tests pass；
- `project_scope_cli_smoke.py --require-claude --require-codex` pass；
- long-context pressure smoke pass；
- `agent_planner_acceptance.sh --full` pass；
- Gateway 服务目录 marker 不得泄漏到下游结果；
- 当前运行配置明确启用长上下文增强，并保持用户侧工具下游执行。

