# Strict Cloud Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 校准正式验收与实际运行配置，使独立 Gateway 对弱上游提供真实下游工具协议和类无限上下文能力。

**Architecture:** 保持 Gateway-owned 与 downstream-owned 工具边界；只修复漂移的 CLI smoke 和实际服务 context 开关，不改动已经通过测试的协议/编排核心。CLI smoke 使用真实 Codex/Claude Code 验证用户侧执行，Gateway direct endpoint 只验证 Gateway-owned 工具。

**Tech Stack:** Python 3.10、标准库 HTTP server/urllib、pytest、Codex CLI、Claude Code CLI、JSON 配置。

## Global Constraints

- 默认上游不支持 tools/function calls：`tools_enabled=adapter`。
- `execute_user_side_tools_in_gateway=false`。
- 不调用公网真实上游；验收使用自包含 mock chat-only upstream。
- 不泄漏或提交 `.gateway_service.json` 中的密钥。
- 不重构无关模块。

---

### Task 1: 校准严格云端 CLI smoke

**Files:**
- Modify: `tests/integration/project_scope_cli_smoke.py`

**Interfaces:**
- Consumes: `/v1/tools/call` Gateway-owned direct endpoint、`/anthropic/v1/messages`、`/v1/responses`、`run_claude()`、`run_codex()`。
- Produces: strict-cloud summary fields and a zero exit status only when protocol delegation and both real CLIs pass。

- [ ] **Step 1: 保留现有失败作为基线**

Run:

```bash
python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
```

Expected: FAIL at direct `Skill` with HTTP 400 because user-side direct execution is disabled.

- [ ] **Step 2: 删除用户侧 direct execution 假设**

移除 direct `Skill`、`Read`、`/v1/functions/call` Read 检查；保留 `SaveMemory`/`RecallMemory` Gateway-owned 隔离检查，并新增 direct calculator 检查：

```python
calculator = tool(base_url, key, "calculator", {"expression": "20+22"}, project_root, run_dir / "direct_calculator.json")
summary["gateway_owned_calculator_ok"] = content(calculator) == "42"
```

- [ ] **Step 3: 收紧 streaming 协议断言**

Anthropic 必须包含 `tool_use` 和 `Skill`；Responses 必须包含 `function_call` 和 `Skill`，不再允许项目 marker 文本代替协议事件。

- [ ] **Step 4: 更新 required 字段并运行正式双 CLI smoke**

Run:

```bash
python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
```

Expected: exit 0, `pass=true`, `claude_skill_ok=true`, `codex_skill_ok=true`.

### Task 2: 启用实际服务长上下文

**Files:**
- Modify: `.gateway_service.json` (gitignored local runtime config)

**Interfaces:**
- Consumes: `context` and `gateway` configuration read by `gateway_config.py`/`gateway_context.py`。
- Produces: actual runtime configuration with context enhancement enabled and downstream tool ownership preserved。

- [ ] **Step 1: 修改四个 context 开关**

```json
{
  "context": {
    "enabled": true,
    "fanout_enabled": true,
    "quality_review_enabled": true,
    "memory_enabled": true
  }
}
```

- [ ] **Step 2: 验证安全边界**

```python
assert config["gateway"]["execute_user_side_tools_in_gateway"] is False
```

Expected: all five assertions pass without printing secrets.

### Task 3: 回归与完成审计

**Files:**
- Modify only if evidence finds a target-related defect.

**Interfaces:**
- Consumes: Task 1 smoke and Task 2 runtime config。
- Produces: requirement-by-requirement completion evidence。

- [ ] **Step 1: 运行 focused tests**

```bash
python3 -m pytest -q tests/test_config_sync.py tests/test_context_enhanced.py
```

Expected: PASS.

- [ ] **Step 2: 运行长上下文压力 smoke**

```bash
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
```

Expected: `ok=true`, compaction and cross-tenant checks true.

- [ ] **Step 3: 运行全量 acceptance**

```bash
./scripts/agent_planner_acceptance.sh --full
```

Expected: focused gate and full pytest both pass.

- [ ] **Step 4: 检查 diff/config/secret boundary**

```bash
git diff --check
git status --short
```

Expected: only requested tracked files changed; `.gateway_service.json` remains ignored and no secret is staged.

