# Gateway SQLite memory / 无限上下文方案

目标：在上游 API token/上下文有限、且上游不稳定支持 tool_calls 的情况下，Gateway 自己承担会话记忆、上下文压缩、分片分析和真实本地工具执行。

## 已实现

1. **SQLite 会话记忆**
   - 表：`conversation_memories`
   - 字段包含 `session_key`、`workspace_root`、`kind`、`summary`、`keywords_json`、`importance`、`last_used_at`。
   - 默认使用 SQLite WAL，不写高频 JSONL 日志。
   - 按 `session_key + workspace_root` 隔离，避免不同项目/会话串记忆。`workspace_root` 使用当前请求解析出的下游项目根；新写入保存可审计的真实路径，召回/列表兼容旧版 hash key，但不会跨项目放宽。

2. **记忆提取**
   - 每次 `/v1/chat/completions`、`/v1/messages`、`/v1/responses` 成功返回后，提取 compact summary。
   - 不保存超大原文；长文本会变成头尾摘要，并带 `gateway context compacted` 标记。
   - 自动识别代码分析、实现/修改/测试类请求并提升 `importance`。

3. **记忆召回**
   - 新请求进入 orchestration 前，根据当前用户文本关键词召回相关记忆。
   - 召回内容注入 system/instructions，标明来自 `Gateway recalled memory`，只作为上下文，不替代实时工具验证。
   - 注入长度受 `memory_inject_max_chars` 限制。

4. **无限上下文组合策略**
   - 短上下文：直接召回 SQLite 记忆 + 本地 planner 工具证据。
   - 大上下文：`context_fanout` 分片并发请求上游，最后综合 + 质量审查。
   - 项目分析：默认先生成下游用户侧 `Tree/Glob/PythonSymbols/ReadManyFiles` 工具请求；显式本地代理模式才由 Gateway 先收集证据再交给上游总结。

## 配置项

`scripts/mimo_gateway.sh` 默认开启：

```bash
GATEWAY_MEMORY_ENABLED=1
GATEWAY_MEMORY_MAX_ITEMS=200
GATEWAY_MEMORY_RECALL_LIMIT=8
GATEWAY_MEMORY_INJECT_MAX_CHARS=4000
GATEWAY_MEMORY_SUMMARY_MAX_CHARS=900
GATEWAY_CONTEXT_ENABLED=1
GATEWAY_CONTEXT_FANOUT_ENABLED=1
```

## 验证

已覆盖单测：

- 同 session/workspace 可召回记忆。
- 不同 session 不串记忆。
- 超大请求只存摘要，不存原文。
- SQLite-only logging 默认不写 `.gateway_requests.jsonl` / `.gateway_tool_failures.jsonl` / `.gateway_stats.json`。

已覆盖 smoke：

- 任意目录 project analyze/modify/run。
- Read/Write/Edit/Bash/code_interpreter/WebFetch/WebSearch/AnalyzeImage/IntentDetect/parallel tools。
- SQLite 日志未回退文件写入。

## 当前边界

- `computer_use`、`click`、`type_text`、`press_key`、`scroll` 已接真实本地后端（macOS Quartz / pyautogui 等）；没有桌面权限、显示环境或可选依赖时会失败并记录，不伪装成功。
- `image_generation` 只使用真实 provider（OpenAI / Pollinations / Hugging Face）；所有 provider 都失败时返回失败/`connector_required`，不会生成本地 placeholder。
- 已将更多常见兼容工具改为真实本地实现：`LSP`、`WebBrowser`、`file_search_call`、`web_search_call`、`web_search_preview_2025_03_11`、`spawn_agent/send_input/wait_agent/close_agent/resume_agent`、`request_user_input`、`TeamCreate/SendMessage/TeamDelete`、MCP resource aliases。

## 持续运行 / 后台常驻

当前只保留两个用户脚本，避免脚本面过宽导致验证分散：

```bash
# 启动/停止/查看 Gateway 服务和 Web UI
./scripts/mimo_gateway.sh start
./scripts/mimo_gateway.sh status
./scripts/mimo_gateway.sh restart
./scripts/mimo_gateway.sh logs
./scripts/mimo_gateway.sh stop

# 一键启动/复用 Gateway，然后按 claude_m1 环境变量启动 Claude Code
./scripts/claude_m1.sh
```

`mimo_gateway.sh` 默认端口 `8885`，默认上游由环境变量或本地 `.gateway_service.json` 配置，默认模型 `mimo-v2.5-pro`，默认下游 key 由 `GATEWAY_DOWNSTREAM_KEY` 环境变量设置（必填），默认 SQLite 日志 `gateway_log.sqlite3`。

服务脚本默认优先用 `screen + pidfile + healthz` 后台运行（无 screen 时回退 `nohup`），并在启动时处理端口占用；如需强制 nohup 可设置 `GATEWAY_START_METHOD=nohup`。当前验收以这两个脚本为准。

## 并发能力

Gateway 使用 `ThreadingHTTPServer` 处理下游并发请求，并有两层并发控制：

- `gateway.max_concurrent_requests` / `GATEWAY_MAX_CONCURRENT_REQUESTS`：全局下游请求并发阀门，默认 32。
- `context.fanout_max_workers` / `GATEWAY_CONTEXT_FANOUT_MAX_WORKERS`：超大上下文 fanout 子请求并发，默认 4。
- `upstream.max_concurrency`：上游能力配置项，用于标注/未来路由扩展。
- SQLite 使用 WAL + busy timeout，支持高频并发写日志/统计/记忆。

并发压测脚本：

```bash
./tests/integration/stress_gateway_concurrency.py --workers 16 --direct-tool-requests 32 --model-requests 1
```

已验证 34 个并发请求（tools/token/chat/messages 混合）全部成功。

## Top tool/function compatibility expansion

新增一批常见 coding-agent / OpenAI / Claude Code / MCP 兼容入口：

- Shell lifecycle aliases: `BashOutput`, `KillBash`, `bash_output`, `kill_bash`。
- Direct call shape: 支持 `recipient_name: functions.ToolName`，以及顶层 `tool_uses` 自动路由到 `multi_tool_use.parallel`。
- MCP generic tools: `mcp_list_tools` / `list_mcp_tools` / `McpListTools`，`mcp_call_tool` / `call_mcp_tool` / `McpCallTool`。
- Memory tools: `Memory` / `SaveMemory` / `RecallMemory` / `remember`，真实读写 SQLite compact memory；默认只列出当前下游项目根的记忆，只有显式 `all_workspaces` / `include_all_workspaces` 才做全局审计列表。
- Skill aliases: `read_skill` / `run_skill` / `list_skills`。
- Web/file aliases: `web_search_preview_2025_03_11`、`file_search_call`、`WebBrowser` 等继续映射到真实本地执行器。

GUI / image 工具不伪装成功：`computer_use`、`click`、`type_text`、`press_key`、`scroll` 走真实本地后端；`image_generation` 走真实图片 provider。依赖、权限、显示环境或 provider 不可用时会作为失败进入 SQLite failures，后续可通过 MCP/plugin connector 扩展。

## Web client config center

Gateway now exposes a protected client-configuration center:

- `/client-config` — HTML copy center for downstream clients.
- `/client-config.json` — machine-readable snippets.
- Admin auth required; default is `admin/admin` for development/testing only, must be changed via `GATEWAY_ADMIN_PASSWORD` in production.
- It only generates copyable snippets and does **not** write to `~/.codex`, `~/.claude`, `opencode.json`, or `.bash_profile`.

Generated downstream snippets include:

- Codex `~/.codex/config.toml`
- Codex `~/.codex/auth.json`
- OpenCode `opencode.json`
- Claude Code `.bash_profile` function `claude_m1`
- Claude Code terminal env exports
- VSCode Claude Code `~/.claude/settings.json`

Configurable fields:

- Gateway public base URL
- Downstream API key
- model / review model
- Codex reasoning effort
- client context window
- auto-compaction token limit
- output token limit

Strict verification command:

```bash
./scripts/mimo_gateway.sh verify
```

This runs compile checks, unit tests, security checks, functional smoke, and concurrency/performance stress.

## Too-long forced fan-out

当上游没有直接报错，而是用自然语言返回 `text too long`、`send it in parts`、`内容过长`、`上下文` 等拒绝文本时，Gateway 会把它识别为上下文拒绝，并基于原始请求触发 forced fan-out：

1. 按 `fanout_chunk_tokens` 切片。
2. 并发请求上游做子分析。
3. 发送综合请求。
4. 可选质量审查。
5. 返回带 `gateway_context.strategy=fanout_forced_synthesis` 的最终答案。

这样 Claude Code 看到的是最终分析结果，而不是 “请简化内容/分段发送”。

### 反思修正：综合阶段不能重塞完整原文

一次失败模式是：Gateway 已经触发 fan-out，但 `_make_synthesis_prompt` 又把完整 `original_prompt` 放进综合请求，导致综合请求再次超过上游上下文窗口。

当前修正：

- forced fan-out 时把 chunk token 上限压到更小值。
- synthesis prompt 只带 `原始用户问题（压缩）`。
- 子分析结果按总预算裁剪，每个 partial 有独立上限。
- quality review 同样只带压缩原始问题和压缩草稿。
- 文本工具回退阶段会清洗路径类参数：如果弱模型输出 `README.md\n<tool_call>` 或 `src/app.py\n\n---审查报告`，Gateway 只把第一个真实路径传给本地工具。

这保证“无限上下文”不是把原文反复塞给上游，而是分层摘要和证据归并。
