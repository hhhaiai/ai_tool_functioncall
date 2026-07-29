# Gateway 上下文、SQLite Memory 与 Fan-out

> 最后校准：2026-07-29。“无限上下文”指 Gateway 通过有界压缩、记忆召回、分片和综合扩大可处理输入范围，不表示上游模型拥有真正无限 token 窗口。

## 目标

当上游上下文有限或对超长输入不稳定时，Gateway 在请求进入上游前完成：

1. token/字符预算估算；
2. 历史消息压缩和摘要；
3. 按 tenant/session/workspace 召回 SQLite memory；
4. 超长输入 fan-out 分片并行分析；
5. 对 partial 结果做有界综合；
6. 成功响应后写入 compact memory。

主要实现：

- `src/gateway_context.py`：估算、压缩、memory、fan-out 和综合；
- `src/gateway_agent_planner.py`：planner session/event 和周期 rollup；
- `src/gateway_sqlite.py` / `src/gateway_persistence.py`：共享 SQLite 与持久化；
- `src/gateway_maintenance.py`：retention、bounded cleanup、incremental vacuum；
- `src/gateway_streaming.py`：流式路径的同一 workspace/memory 边界。

## 隔离键

Conversation memory 至少按以下上下文隔离：

```text
authenticated downstream tenant/client
+ planner/session identity
+ resolved downstream workspace root
```

请求体提供的任意 `client_id` 不能越过已认证租户；`all_workspaces` / `include_all_workspaces` 不能让普通 direct tool 枚举其他项目。跨租户全局查询只允许 Admin API。

workspace 来源：

1. `workspace_root` / `gateway_workspace`；
2. Claude Code `Primary working directory` / `Worktree`；
3. Codex `<environment_context><cwd>`；
4. metadata 中受支持的项目目录字段；
5. env/config 的显式本地代理设置；
6. 匿名隔离空间。

不会使用 Gateway 服务启动目录作为普通请求的隐式项目根。

## Memory 生命周期

### 写入

成功对话会提取 compact summary，而不是保存无限原文。长内容被裁剪为头尾或摘要，并带压缩标记。代码分析、实现、修改和测试类信息可提高 importance。

### 召回

新请求按当前用户文本和 scope 检索相关 memory，注入 system/instructions。召回内容只作为历史上下文，不能替代当前文件、工具或 runtime 验证。

### Rollup 和维护

长会话按配置周期 rollup；后台维护按天数和最大行数做有界清理，并使用 incremental vacuum，避免在线执行破坏性 full vacuum。

## Fan-out

Fan-out 在以下场景使用：

- 输入预算在上游发送前已超限；
- 上游返回可识别的上下文过长错误；
- 上游以自然语言拒绝超长输入并要求分段。

流程：

```text
original request
  → bounded chunks
  → parallel upstream partial analyses
  → trim each partial and total synthesis budget
  → final synthesis
  → optional quality review helper
```

综合阶段只带压缩后的原始问题和有界 partial，不会把完整原文重新塞回上游。

## 当前默认配置

当前模板中的主要默认值：

```json
{
  "context": {
    "enabled": true,
    "fanout_enabled": true,
    "fanout_chunk_tokens": 120000,
    "fanout_max_chunks": 0,
    "fanout_max_workers": 4,
    "memory_enabled": true,
    "memory_max_items": 200,
    "memory_recall_limit": 8,
    "memory_inject_max_chars": 4000,
    "memory_summary_max_chars": 900,
    "memory_rollup_every_turns": 8,
    "memory_rollup_max_chars": 4000
  }
}
```

`fanout_max_chunks=0` 表示不额外设置块数上限，实际仍受请求大小、并发、上游 timeout、输出预算和全局准入限制。

相关环境变量：

```text
GATEWAY_CONTEXT_FANOUT_ENABLED
GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS
GATEWAY_CONTEXT_FANOUT_MAX_CHUNKS
GATEWAY_CONTEXT_FANOUT_MAX_WORKERS
GATEWAY_MEMORY_ENABLED
GATEWAY_MEMORY_MAX_ITEMS
GATEWAY_MEMORY_RECALL_LIMIT
GATEWAY_MEMORY_INJECT_MAX_CHARS
GATEWAY_MEMORY_SUMMARY_MAX_CHARS
GATEWAY_MEMORY_ROLLUP_EVERY_TURNS
GATEWAY_MEMORY_ROLLUP_MAX_CHARS
GATEWAY_MEMORY_RETENTION_DAYS
GATEWAY_MEMORY_MAX_ROWS
```

## Streaming parity

流式与非流式入口使用同一请求级 workspace scope、memory 召回和压缩边界。Responses 的 `input`、Chat/Messages 的 `messages` 都会在预算阶段处理；client disconnect 会取消上游流。

chat-only 工具改写为了正确性可能先缓冲工具决策轮，但最终 SSE 仍遵守下游协议事件格式。

## 当前边界

- memory 是摘要和相关召回，不是无损数据库备份；
- fan-out partial 可能受上游模型质量影响；
- 单次请求仍受 HTTP body、token、timeout、并发、数据库和输出限制；
- 外部 Provider 的真实上下文上限必须以其 live 行为验证；
- quality/reflection helper 存在，但响应后自动二次 LLM 重写尚未接生产主链。

## 验证

```bash
python3 -m pytest -q tests/test_context_enhanced.py tests/test_cache_persistence.py tests/test_persistence.py
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
./scripts/agent_planner_acceptance.sh --full
```
