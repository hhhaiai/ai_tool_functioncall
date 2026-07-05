# Gateway 项目进度跟踪

> 最后更新: 2026-06-27

## 项目概述

AI Gateway 中游服务 - 将各种上游 API（不支持/部分支持 tool calls）封装为完整支持 tool calls 的统一接口，支持无限上下文、智能缓存、协议转换。完美支持下游 Claude Code / Codex 用户。

## 功能进度

### 1. 无限上下文 (Infinite Context) ✅ DONE

**状态**: 已完成
**模块**: `src/gateway_context.py`
**测试**: `tests/test_context_enhanced.py` (34 通过)

已实现:
- Token 估算 (`_approx_token_count`, `_body_token_estimate`)
- 消息压缩 (`_compact_messages`, `_compact_messages_with_summary`)
- 文本分块 (`_chunk_text_by_tokens`)
- 文本裁剪 (`_trim_text_for_context`)
- 扇出并行 (`_should_fanout_context`, `_make_synthesis_prompt`)
- 记忆系统 (`_memory_*` 系列函数) - **智能搜索已接入** (`_smart_memory_search` 替代简单关键词匹配)
- 系统提示注入 (`_inject_gateway_system_prompt`)
- **上下文压缩默认启用** (`_context_enabled` 默认 True)

---

### 2. Tool Calls / Function Calls ✅ DONE

**状态**: 已完成
**模块**: `src/gateway_tool_runtime.py`, `src/gateway_builtin_tools.py`
**测试**: `tests/test_tool_parallel.py` (25 通过)

已实现:
- 工具调用解析 (`_extract_tool_calls`, `_parse_text_tool_calls`)
- 工具调用规范化 (`_normalize_tool_call`)
- 内置工具执行器 (`BUILTIN_TOOLS` dict, 50+ 工具)
- Claude Code 兼容层 (`src/gateway_claude_compat.py`)
- 并行执行策略 (读工具并行, 写工具串行)
- 工具依赖分析
- **工具结果缓存** (读工具自动缓存, 30s TTL)

---

### 3. 智能缓存 (Semantic Caching) ✅ DONE

**状态**: 已完成
**模块**: `src/gateway_cache.py`
**测试**: `tests/test_semantic_cache.py` (42 通过)

已实现:
- 语义缓存 (`SemanticCache` class) - **已接入请求流程** (非流式请求自动缓存)
- 嵌入向量提供者 (`LocalEmbeddingProvider`, `RemoteEmbeddingProvider`)
- 工具结果缓存 (`ToolResultCache` class) - **已接入工具执行** (读工具自动缓存)
- 余弦相似度计算 (`cosine_similarity`)
- LRU 淘汰策略
- 缓存失效机制 (`invalidate` by path)
- **本地嵌入阈值优化** (0.75，适配粗粒度本地嵌入)

---

### 4. Web2API ✅ DONE

**状态**: 已完成
**模块**: `src/gateway_web2api.py`
**测试**: `tests/test_web2api.py` (39 通过)

已实现:
- Web2ApiEngine class
- HTML 解析器 (`SimpleHTMLExtractor`)
- CSS 选择器提取 (`_simple_css_select`)
- 正则表达式提取 (`_regex_extract`)
- 元数据提取 (`_extract_meta_content`, `_extract_title`, `_extract_links`)
- 自动提取模式

---

### 5. 智力提升 (Intelligence Enhancement) ✅ DONE

**状态**: 已完成
**模块**: `src/gateway_intelligence.py`
**测试**: `tests/test_intelligence.py` (71 通过)

已实现:
- 问题分析 (`_analyze_question`) - 规则分析 + LLM 分析（需配置 gateway_llm.py）
- 复杂度检测 (语义信号，非硬编码规则)
- 领域识别 (代码/数学/创意/事实/通用)
- 问题分解 (`_decompose_question`)
- 反思机制 (`_generate_reflection`) - **流式/非流式路径均已接入**
- 回答质量评估 (`_assess_answer_quality`)
- 完整性/相关性/清晰度/准确性评分
- 增强系统提示构建 - **英文提示**（原中文已改为英文）
- 反思提示生成 - **英文提示**
- **流式/非流式路径一致性** - 两者都应用 system_prompt + reflection_prompt

---

### 6. Web 配置界面 (Admin UI) ✅ DONE

**状态**: 已完成
**模块**: `src/gateway_admin.py` (主 UI), `src/gateway_web_config.py` (配置编辑)
**测试**: `tests/test_web_config.py` (41 通过)

#### 主管理界面 (/ui) - 5 Tab 布局
- **📊 Dashboard** - 活跃模型、上游状态、能力矩阵、Gateway 配置、上下文配置
- **🔧 Models** - 上游 profiles 管理 (添加/激活/删除)、downstream keys、配置编辑
- **📖 Usage** - 客户端接入指南 (Codex CLI, Claude Code, OpenCode, VS Code)
- **🛠 Tools & Skills** - MCP 服务器管理、内置工具列表、**Skills 列表与查看**、HTTP Actions
- **📋 Logs** - 请求日志、失败日志、对话记忆、统计信息、脱敏配置

#### Skills API (新增)
- `GET /admin/skills.json` - 列出所有已安装 skills (扫描 workspace + user-global 目录)
- `GET /admin/skill-content.json?name=<name>` - 读取 skill 内容 (SKILL.md)
- Skills 支持: workspace-local (`.codex/skills`, `.agents/skills`, `.claude/skills`) + user-global (`~/.codex/skills` 等)
- UI 中可点击 View 查看 skill 内容 (弹窗显示)

#### 配置编辑界面 (/ui/config) - 9 Tab
- 上游配置、能力配置、上下文配置、并发配置、缓存配置、工具配置、Web2API、安全配置、配置导出

---

### 7. 问答统计 (Q&A Statistics) ✅ DONE

**状态**: 已完成
**模块**: `src/gateway_stats.py`
**测试**: `tests/test_stats.py` (35 通过)

已实现:
- 请求统计 (成功率、响应时间、token 使用)
- 工具调用统计 (使用频率、失败率、执行时间)
- 缓存统计 (命中率、相似度)
- 质量统计 (完整性、相关性、清晰度、准确性)
- 上游统计 (各上游成功率、响应时间)
- 综合仪表板 (`get_dashboard`)
- 趋势分析 (`get_hourly_trends`)
- Top 查询分析 (`get_top_paths`, `get_top_tools`)
- CSV 导出功能
- 数据清理 (`cleanup_old_stats`)

---

### 8. 并发优化 (Concurrency Optimization) ✅ DONE

**状态**: 已完成
**模块**: `src/gateway_concurrency.py`
**测试**: `tests/test_concurrency.py` (37 通过)

已实现:
- HTTP 连接池 (`ConnectionPool`)
- 负载均衡器 (`LoadBalancer`)
  - 轮询策略 (round_robin)
  - 最少连接策略 (least_connections)
  - 随机策略 (random)
- 请求队列 (`RequestQueue`)
- 并发请求执行器 (`ConcurrentRequestExecutor`)
- 多上游管理器 (`MultiUpstreamManager`)
- 健康检查 (`UpstreamHealth`)
- 自动重试机制
- 故障转移支持

---

## 测试状态

| 测试文件 | 状态 | 数量 |
|----------|------|------|
| tests/test_gateway.py | ✅ PASS | 209 |
| tests/test_edge_cases.py | ✅ PASS | 126 |
| tests/test_intelligence.py | ✅ PASS | 71 |
| tests/test_web2api.py | ✅ PASS | 47 |
| tests/test_context_enhanced.py | ✅ PASS | 47 |
| tests/test_semantic_cache.py | ✅ PASS | 42 |
| tests/test_web_config.py | ✅ PASS | 41 |
| tests/test_concurrency.py | ✅ PASS | 40 |
| tests/test_stability.py | ✅ PASS | 37 |
| tests/test_stats.py | ✅ PASS | 35 |
| tests/test_claude_compat.py | ✅ PASS | 33 |
| tests/test_tool_parallel.py | ✅ PASS | 25 |
| tests/test_stats_logging.py | ✅ PASS | 16 |
| tests/integration/test_gateway_e2e.py | ✅ PASS | 15 |
| tests/test_tool_execution_trace.py | ✅ PASS | 9 |
| **总计** | **✅ ALL PASS** | **886 passed, 2 skipped** |

> **注意**: 之前 macOS/urllib 代理污染导致的本机 HTTP `RemoteDisconnected` 已在 2026-06-19 修复；当前全量回归为 886 passed, 2 skipped。

---

## 最近修复

### 2026-06-26: Chat-only 真实上游适配到 Codex / Claude Code 原生工具协议

**目标确认**: 上游 `http://47.85.40.209:8885` 只按普通对话稳定工作；实测会接受 `tools` 字段但忽略工具，不返回协议级 `tool_calls/function_call/tool_use`。Gateway 必须在外层补齐真实协议字段，让 Codex / Claude Code 像连接原生 tools 模型一样执行本地文件、shell、skills 等用户侧工具。

**本轮实现/配置:**
- ✅ 上游能力已实测：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、三类 stream 均可用；native tools/function calls 不可用。
- ✅ `.gateway_service.json` 已接入真实上游，密钥只保存在 gitignored 本地配置并加密；`gateway.config.json` 只保留无密钥模板。
- ✅ 上游配置校正为 `tools_enabled=adapter`、`native_tools_verified=false`、`supports_tools=false`、`supports_function_calls=false`、`supports_streaming=true`。
- ✅ Gateway 会在调用上游前合成协议级工具轮次：OpenAI Chat 返回 `message.tool_calls`，Responses 返回 `output[].type=function_call`，Anthropic Messages 返回 `content[].type=tool_use`。
- ✅ 针对真实客户端 schema 做参数适配：Claude Code `Skill(skill=...)` / `Read(file_path=绝对路径)`；Codex Responses `exec_command(cmd=...)`；没有 LS/Glob 时自动退到 shell 命令收集项目结构。
- ✅ 用户机器工具默认仍由下游执行，Gateway 不直接碰用户本地文件/shell；语义缓存不缓存 tool request 回合，避免复用旧工具名/旧参数。
- ✅ `tests/integration/project_scope_cli_smoke.py` 已升级为 chat-only 上游闭环烟测，可要求真实 Claude Code / Codex CLI 执行本地工具并把结果回传给 chat-only 上游。
- ✅ 配置后台入口已恢复兼容：`/ui` 仍是主入口，`/config`、`/admin`、`/admin/config-ui` 现在也会打开同一个 Gateway Control Center，避免旧书签看起来“后台不见了”。
- ✅ 对齐原生 tools API 的项目分析起手式：当 Claude Code 声明 `Skill` 且上下文列出 `codebase-onboarding` 时，`分析这套项目` 优先下发 `Skill(codebase-onboarding)`；弱上游后续若只说“gather project structure/key files”，会继续下发声明过的 `Bash` 检索命令，不再误造不存在的 `LS(path="to")`。

**验证:**
```bash
python3 -m py_compile src/toolcall_gateway.py src/gateway_app.py src/gateway_tool_runtime.py src/gateway_streaming.py src/gateway_protocol.py src/gateway_http_handler.py tests/integration/project_scope_cli_smoke.py
# OK

python3 -m pytest -q
# 896 passed, 2 skipped, 21 warnings

NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass: true; claude.ok=true; codex.ok=true

# 真实上游适配烟测
# run_dir: .gateway_runtime/real-upstream-adapter-smoke-20260625-234218
# models/chat/stream/messages_tool_use/responses_exec/custom_tool/Claude CLI/Codex CLI 全部 ok
```

**方案文档**: `docs/chat-only-upstream-tool-adapter.md`

---

### 2026-06-19: Gateway 最终可用性收敛与精简

**目标确认**: 本项目是中游 Gateway：上游可以是不支持/弱支持 tools/function calls 的普通 API；下游面向 Claude Code / Codex / SDK。Gateway 必须保持协议正确、不能污染普通请求、不能把服务启动目录误当用户项目目录。

**本轮修复/精简:**
- ✅ 修复 macOS/urllib 系统代理污染：新增 `src/__init__.py`，强制 `127.0.0.1/localhost/::1` 走本机直连，避免 mock upstream/Admin/Web2API 被代理导致 `RemoteDisconnected`。
- ✅ 普通无 tools 请求保持普通转发：弱上游 text adapter 只在下游确实提交 `tools/tool_choice`（或压缩前请求含 tools）时注入工具协议说明，避免简单 chat 被塞入大段 Gateway 工具手册。
- ✅ workspace root 边界收紧：默认不再保存/回退 Gateway 服务 cwd；优先级为请求显式 root / 下游 metadata / `GATEWAY_WORKSPACE_ROOT` / 显式配置 root / 匿名隔离空间。Admin skills 页面在没有 workspace 时只展示全局/额外 skills，不再 500。
- ✅ 工具执行归属收敛：Gateway-owned 工具（HTTP Action/MCP/WebFetch/WebSearch/calculator/Memory 等）仍由 Gateway 真执行；用户侧机器工具（Read/LS/Glob/Grep/Write/Edit/Bash/Skill/GUI/local agent）默认转成下游原生 tool request，由 Claude Code/Codex 在用户机器执行并回传结果。
- ✅ 弱上游文本工具、强制 `tool_choice`、local planner、streaming adapter 共用同一归属判断；例如“查看当前目录”会返回 `LS` tool_use，不会列出 Gateway 仓库；天气 HTTP Action 会真实请求配置的天气接口。
- ✅ 匿名 workspace 按 `metadata.user_id` 内 JSON 的 `session_id` 稳定分配，SQLite 记忆召回在同 session 下恢复一致。
- ✅ 配置加密精简：`password_hash` / `key_hash` 保持稳定 hash，不再二次 Fernet 加密；明文 `admin.password` 仍会归一化为 hash 且不持久化。
- ✅ Bash 弱文本工具参数同时兼容 `command` 与 `cmd`，修复弱上游 `<function=Bash>` markup 解析后的参数不一致。
- ✅ 清理本地生成物：删除 `__pycache__`、`.pytest_cache`、`.ruff_cache`、`.DS_Store`；保留 `.gateway_runtime/`、`gateway_log.sqlite3`、`.gateway_service.json` 等可能含本地状态/密钥/日志的文件。

**验证:**
```bash
python3 -m compileall -q src tests
# OK

python3 -m pytest -q
# 886 passed, 2 skipped

# 临时端口本地 mock smoke
# OK: healthz, /v1/models, chat, direct calculator, user-side LS delegation
```

---

### 2026-06-18: Tool Call 默认行为修复 — 所有上游 API 默认使用 Text Adapter 模式

**问题**: Gateway 默认假设上游 API 支持原生 tool calls (`supports_tools=True`, `supports_function_calls=True`)，导致普通 API（不支持 tool call）收到带 tools 的请求后返回 400 或静默忽略工具。

**修复**: 将 7 处默认值从 `True` 改为 `False`：
- `gateway_streaming.py:_upstream_native_tools_capable()` — 2 处
- `gateway_context.py:_upstream_supports_native_tools()` — 1 处
- `gateway_config.py:_profile_from_admin_form()` — 2 处
- `gateway_tool_runtime.py:_text_tool_call_fallback_enabled()` — 1 处
- `gateway_tool_runtime.py:_weak_upstream_text_tools_active()` — 1 处
- `gateway_tool_runtime.py:_run_tool_orchestration_scoped()` delegate logic — 1 处

**效果**:
- ✅ 所有上游 API 默认使用 Text Adapter 模式（不发送原生 tools，改为注入文本提示）
- ✅ Gateway 自动从上游文本响应中提取 tool calls；gateway-owned 工具本地执行，用户侧机器工具下发给下游客户端执行
- ✅ 显式配置 `capabilities.supports_tools: true` 仍可启用原生 tool call 透传
- ✅ 新增 6 个测试 (`ToolCallDefaultTests`)

**新增测试**: `tests/test_gateway.py::ToolCallDefaultTests` (6 通过)

---

### 2026-06-18: Tool Call 完整性修复与代码审查

**修复内容** (3 CRITICAL + 6 HIGH):

**CRITICAL:**
1. **重复函数定义** - `_parse_text_tool_calls` 在 `gateway_tool_runtime.py` 中定义了两次，第二个定义覆盖第一个。删除了重复定义。
2. **权限检查 fail-open** - 权限检查异常时默认允许执行。改为 fail-closed：非 import 异常拒绝执行。
3. **finish_reason 未映射** - Anthropic `tool_use` 直接传递为 OpenAI `finish_reason`，导致下游客户端无法检测工具调用。添加了正确的映射。

**HIGH:**
4. **DEBUG print 语句** - 17 个 `print(..., file=sys.stderr)` 替换为 `_logger.debug()`
5. **死变量清理** - `_detect_intent_tool_calls` 中 `last_user_text`、`text_lower` 赋值但未使用
6. **thinking blocks 合并** - 多个 thinking blocks 被最后一个覆盖，改为累积
7. **usage 数据丢失** - Anthropic→OpenAI 转换缺少 token 使用量，已添加
8. **异常重试范围过宽** - `except Exception` 重试所有异常，改为只重试 transient 错误
9. **400 重试丢失工具定义** - 上游返回 400 时工具列表被清空，改为保留原始工具列表

**流式路径修复:**
- 添加了 SSE 错误处理包装（防止 SSE headers 发送后异常导致连接断开）
- 添加了 `_logger` 到 `gateway_streaming.py`
- 修复了 intelligence enhancement 异常吞没

---

### 🔴 2026-06-16: CRITICAL SECURITY FIX - Workspace 路径遍历漏洞

**严重性**: CRITICAL (CVSS 9.8)
**漏洞类型**: 路径遍历 / 未授权文件访问

**问题**: Gateway 在客户端未提供 `workspace_root` 时，会回退到**服务器目录**，导致攻击者可以读取服务器上的任意文件（`/etc/passwd`、SSH 密钥、配置文件等）

**当前修复状态（已被 2026-06-19 收敛更新覆盖）**:
- ✅ `_request_workspace_root()` 不再回退 Gateway 服务 cwd；缺失客户端 workspace 时使用匿名隔离空间，避免服务目录泄露。
- ✅ 用户侧机器工具默认不在 Gateway 服务机执行，而是返回下游原生 tool request。
- ✅ 只有显式 `GATEWAY_WORKSPACE_ROOT` / 配置 root / `gateway.execute_user_side_tools_in_gateway=true` 等本地代理式部署才允许 Gateway 使用本机 workspace。
- ✅ **绝对不再使用** `os.getcwd()` 或服务器路径作为隐式用户 workspace。

**安全原则**:
- 🔴 **绝对红线**: Gateway 绝不能使用服务器目录作为工作空间
- ✅ 所有 workspace 必须来自客户端（用户本地机器）
- ✅ 如果客户端未提供，必须安全失败（返回错误）

**验证**:
```python
# 无 workspace 时不会返回服务器目录；会分配匿名隔离空间
assert "anonymous_spaces" in str(_request_workspace_root({}))  # ✓ SECURE
```

详见：[SECURITY_FIX_WORKSPACE.md](SECURITY_FIX_WORKSPACE.md)

---

### 2026-06-16: Workspace Root 架构修复

**问题**: `workspace_root` 被持久化保存到配置文件，导致多用户/多项目冲突

**修复**:
- `save_config()` 现在自动移除 `workspace_root` 字段（不再持久化）
- Admin UI 中 `workspace_root` 改为只读显示（运行时值）
- HTTP handler 不再保存用户提交的 `workspace_root`
- 每个请求动态解析 workspace（从 body/metadata 提取）

**优先级链**:
1. 显式字段：`body.workspace_root` 或 `body.gateway_workspace`
2. 客户端 metadata：`metadata.project_dir`, `metadata.cwd` 等
3. 自动提取：从 system/messages 中提取路径（Claude Code 格式）
4. 环境变量：`GATEWAY_WORKSPACE_ROOT`（如果非 cwd）
5. 匿名隔离空间（不会是服务启动 cwd）

**效果**:
- ✅ Gateway 现在是无状态服务（针对 workspace）
- ✅ 支持多用户/多项目同时使用同一个 Gateway 实例
- ✅ 客户端（Claude Code/Codex）自动发送工作目录，无需配置
- ✅ 向后兼容，现有客户端无需修改

详见：[FIX_WORKSPACE_ROOT.md](FIX_WORKSPACE_ROOT.md)

---

### 2026-05-28: 协议转换与缓存修复

### 协议转换修复
- `_from_anthropic_response_to_openai`: thinking blocks 现在保留为 `reasoning` 字段
- `_from_anthropic_response_to_openai`: **多个 thinking blocks 现在累积**（原来只保留最后一个）
- `_from_anthropic_response_to_openai`: **`finish_reason` 正确映射** (`tool_use`→`tool_calls`, `end_turn`→`stop`)
- `_from_anthropic_response_to_openai`: **usage 数据保留**（原来丢失 input_tokens/output_tokens）
- `_openai_messages_to_anthropic`: `reasoning` 字段转换回 `thinking` blocks
- `_openai_messages_to_anthropic`: 连续同角色消息自动合并（Anthropic 要求严格交替）
- `_to_openai_chat_payload`: `stop_sequences` 映射到 OpenAI `stop` 参数
- `_append_tool_results`: `is_error` 字段始终包含（原来只在错误时包含）

### 智能缓存接入
- 语义缓存已接入非流式请求流程（移除了 `not body.get("tools")` 限制）
- 工具结果缓存已接入工具执行流程（读工具自动缓存）
- 本地嵌入相似度阈值从 0.92 降至 0.75（适配粗粒度本地嵌入）
- 测试 fixtures 自动重置缓存（防止跨测试污染）

### 上下文压缩默认启用
- `_context_enabled()` 现在默认返回 True（原来需要显式配置 `context.enabled: true`）

### 记忆系统增强
- `_smart_memory_search` 已接入自动回忆流程（替代简单关键词匹配）
- 支持关键词重叠、文本相似度、时间衰减、重要性加权

### 智力提升修复
- 流式/非流式路径现在一致应用 system_prompt + reflection_prompt
- `use_llm` 默认值统一为 False（原来配置加载默认 True，数据类默认 False）
- 所有提示从中文改为英文（支持双语用户）
- Skills 工具提示从中文改为英文

### Claude Code 兼容性
- WebSearch 工具现在真正执行 DuckDuckGo 搜索（原来返回 stub 错误）
- `_execute_glob` 截断计数 bug 修复

---

## 模块架构

```
src/
├── gateway_app.py              # 入口导出
├── gateway_config.py           # 配置管理
├── gateway_protocol.py         # 协议转换 (OpenAI ↔ Anthropic)
├── gateway_proxy.py            # 上游 HTTP 客户端
├── gateway_context.py          # 上下文压缩/记忆/无限上下文
├── gateway_tool_runtime.py     # 工具执行引擎
├── gateway_builtin_tools.py    # 内置工具实现
├── gateway_cache.py            # 智能缓存 (语义 + 工具结果)
├── gateway_claude_compat.py    # Claude Code 兼容层
├── gateway_web2api.py          # Web2API 转换
├── gateway_intelligence.py     # 智力提升 (问题分析/质量评估)
├── gateway_web_config.py       # Web 配置界面
├── gateway_stats.py            # 问答统计系统
├── gateway_concurrency.py      # 并发优化 (连接池/负载均衡)
├── gateway_computer_use.py     # Computer Use 工具 (截图/鼠标/键盘/图像生成)
├── gateway_streaming.py        # SSE 流式处理
├── gateway_mcp.py              # MCP 协议支持
├── gateway_http_actions.py     # HTTP Action 支持
├── gateway_admin.py            # Admin UI
├── gateway_http_handler.py     # HTTP 入口处理
├── gateway_logging.py          # 日志/统计
├── gateway_errors.py           # 错误处理
├── gateway_encryption.py       # 加密支持
├── gateway_permissions.py      # 工具权限管理
├── gateway_persistence.py      # 持久化支持
├── marketplace.py              # MCP Server 市场目录
└── toolcall_gateway.py         # 兼容性入口
```

---

## 安全注意事项

**敏感文件（已 gitignore，不会提交）：**

| 文件 | 内容 |
|------|------|
| `.gateway_service.json` | 上游 API 地址、密钥、下游 key（本地配置，已 gitignore） |
| `.case.txt` | 测试 curl 命令（含真实 IP） |
| `.gateway_runtime/` | 运行时配置缓存（含真实地址） |
| `.traces/` | Claude Code 调用 trace |
| `.env` | 环境变量 |

**验证命令：**
```bash
# 确认提交代码中无真实 IP
git ls-files | xargs grep -l '47\.85\.40\.209' 2>/dev/null
# 应无输出
```

**原则：**
- 真实 API 地址只放本地配置文件或环境变量，绝不写入提交代码
- Gateway 内部工具使用 `gw_` 前缀，避免与下游用户工具冲突

---

## 运行命令

```bash
# 启动服务
python3 -m src.gateway_app

# 运行全部测试
python3 -c "import pytest, sys; sys.exit(pytest.main(['-v', 'tests/']))"

# 运行特定测试
python3 -c "import pytest, sys; sys.exit(pytest.main(['-v', 'tests/test_intelligence.py']))"
```

---

## 代码审查状态

已完成全模块逐行代码审查 (8 个并行审查 agent):

| 模块 | CRITICAL | HIGH | 状态 |
|------|----------|------|------|
| gateway_tool_runtime.py | 3 | 5 | ✅ 已修复 |
| gateway_intelligence.py | 2 | 6 | ✅ 已修复 |
| gateway_context.py | 2 | 4 | ✅ 已修复 |
| gateway_cache.py | 2 | 3 | ✅ 已修复 |
| gateway_concurrency.py | 5 | 7 | ✅ 已修复 |
| gateway_stats.py | 0 | 5 | ✅ 已修复 |
| gateway_protocol.py | 0 | 5 | ✅ 已修复 |
| gateway_claude_compat.py | 3 | 0 | ✅ 已修复 |
| gateway_builtin_tools.py | 1 | 0 | ✅ 已修复 |

已修复 15 项 CRITICAL/HIGH 问题，详见 [docs/progress/CRITICAL_FIXES.md](docs/progress/CRITICAL_FIXES.md)

---

## 安全状态

- ✅ 命令注入防护 (shell_enabled 配置检查)
- ✅ 路径遍历防护 (workspace root 包含检查)
- ✅ SSRF 防护 (私有/回环 IP 阻止)
- ✅ 线程安全 (RLock, OrderedDict, 连接池计数)
- ✅ 内存安全 (有界缓存, LRU 淘汰)

---

## 商用就绪状态

所有 8 个核心功能已实现并通过测试:

1. ✅ 无限上下文 - 压缩、记忆、扇出并行
2. ✅ 智力提升 - 问题分析、反思、质量评估
3. ✅ Tool Calls - 完整支持，Claude 兼容
4. ✅ Web 配置 - Tab 式管理界面
5. ✅ 问答统计 - 全面的使用统计
6. ✅ 智能缓存 - 语义缓存 + 工具结果缓存
7. ✅ Web2API - 网页转 API
8. ✅ 并发优化 - 连接池、多上游负载均衡

---

## 进度文档

| 文档 | 内容 |
|------|------|
| [docs/progress/STATUS.md](docs/progress/STATUS.md) | 项目总体状态 |
| [docs/progress/CRITICAL_FIXES.md](docs/progress/CRITICAL_FIXES.md) | 15 项 CRITICAL/HIGH 修复记录 |
| [docs/progress/COMPETITIVE_ANALYSIS.md](docs/progress/COMPETITIVE_ANALYSIS.md) | 竞品分析 (LiteLLM, Kong, Portkey 等) |
| [docs/progress/TODO.md](docs/progress/TODO.md) | 待完成工作 (按优先级) |

---

## 集成状态

| 模块 | 集成位置 | 状态 |
|------|---------|------|
| gateway_context.py | run_tool_orchestration / run_streaming_orchestration | ✅ 核心流程 |
| gateway_protocol.py | 所有请求/响应转换 | ✅ 核心流程 |
| gateway_tool_runtime.py | run_tool_orchestration | ✅ 核心流程 |
| gateway_streaming.py | run_streaming_orchestration + 缓存 | ✅ 核心流程 |
| gateway_cache.py | HTTP Handler + 流式缓存 | ✅ 全路径 |
| gateway_intelligence.py | HTTP Handler (非流式请求预处理) | ✅ Handler 层 |
| gateway_stats.py | HTTP Handler (每次请求记录) | ✅ Handler 层 |
| gateway_web_config.py | GET /ui/config, /api/config/* | ✅ 路由层 |
| gateway_web2api.py | POST /api/web2api | ✅ 路由层 |
| gateway_proxy.py | 连接复用 + 重试 | ✅ 核心流程 |
| gateway_claude_compat.py | 导出 (BUILTIN_TOOLS 已覆盖) | ✅ 参考 + 导出 |
| gateway_concurrency.py | 导出 (连接池/负载均衡独立可用) | ✅ 可选增强 |

---

## 待完成 (高优先级)

0. Agent Planner 化 - 当前 tool/function call 仍是 gateway adapter / shim，和原生工具 Agent 差距很大；`分析这套项目` 这类任务需要外层 planner 负责 Skill/MCP/工具多轮编排、状态、证据压缩与最终综合。详见 [docs/agent-planner-gap-analysis.md](docs/agent-planner-gap-analysis.md)
1. 高性能优化 - 替换 ThreadingHTTPServer 为 asyncio/aiohttp (百亿 token/小时)
2. gateway_stats.py 查询优化 (推送聚合到 SQL)
3. Guardrails (输入/输出验证)
4. OpenTelemetry 集成

---

## 2026-06-26 进度校正：原生 Tool API 差距

用户用真正支持 tool 的 API 对比 `分析这套项目` 后确认：当前项目不能再宣称已经达到原生工具 Agent 体验。

已确认差距：

- 原生路径会先触发 `Skill(codebase-onboarding)`，随后调用 codebase-memory-mcp / context-mode，并维护 todo 进度。
- 当前网关历史行为会退化成 `Bash(find ...)`，甚至从上游占位文本中误判出未声明的 `LS(path="to")`。
- 本轮已修复第一步：当下游声明 `Skill` 且上下文包含 `codebase-onboarding` 时，本地网关会返回 `Skill(codebase-onboarding)`。
- 但完整目标是外层 Agent Planner：意图识别、多步计划、工具/Skill/MCP 调度、planner state、evidence compaction、最终 synthesis。

当前验证：

```bash
python3 -m py_compile src/gateway_tool_runtime.py src/gateway_http_handler.py
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_project_analysis_prefers_codebase_onboarding_skill_when_available -q
curl http://127.0.0.1:8885/v1/messages ... # 返回 tool_use: Skill(codebase-onboarding)
```

下一步：新增/拆分 `src/gateway_agent_planner.py`，不要继续只在 `gateway_tool_runtime.py` 里堆特殊正则。

### 2026-06-26 Agent Planner 开发进展

已开始把项目从单纯 tool-call shim 拆成外层 planner：

- ✅ 新增 `src/gateway_agent_planner.py`
  - `PlannerDecision`：描述 planner 下一步工具请求。
  - `AgentPlannerStore`：`.gateway_runtime/agent_planner.sqlite3` 持久化 workflow state / evidence summary。
  - `plan_downstream_tool_request()`：chat-only 上游前置规划，不再等上游说“let me gather...”后才补救。
  - `prepare_upstream_body()`：在最终 synthesis 前注入 compact evidence，明确要求 chat-only upstream 只基于证据总结，不假装调用工具。
- ✅ 非流式 orchestration 已接入 planner：
  - 首轮 `分析这套项目`：优先 `Skill(codebase-onboarding)`。
  - Skill 结果回传后：继续请求真实项目结构工具（优先 code graph / LS+Glob / Bash fallback）。
  - 结构证据回传后：如有 `Read` 工具，读取 README/CLAUDE/AGENTS/manifest/关键源码。
  - 工具证据足够后：把压缩 evidence 注入上游，让 chat-only 模型只做最终表达/分析。
- ✅ streaming orchestration 已复用 planner direct tool request，并在最终 synthesis 前注入 compact evidence。
- ✅ 通用意图已迁入 planner 第一层：
  - 显式 `Skill` 请求；
  - 显式 shell/command 请求；
  - Read/list 这类用户机器工具；
  - web_search / WebSearch；
  - 下游自定义 function（例如 `get_weather(location=...)`）。
- ✅ 复杂 workflow 入口继续扩展：
  - `code_search`：优先下游声明的 codebase-memory MCP `search_graph/search_code`，再退到 Grep/Bash；
  - `test_build`：`运行测试` / `build` / `typecheck` 会生成下游 shell 命令，命令内自动按 `pyproject/tests`、`go.mod`、`package.json` 选择 runner；
  - MCP project 参数可由 `GATEWAY_CODEBASE_MEMORY_PROJECT` / workspace root 自动推断，避免 `get_architecture/search_graph` 缺少 project。
- ✅ 编辑/修复 workflow 已开始接入：
  - `edit`：用户明确给出文件路径和 quoted old/new 时，下发 `Edit(file_path, old_string, new_string)`；
  - `write`：用户明确给出文件路径和 quoted content 时，下发 `Write(file_path, content)`；
  - `fix_loop`：测试/构建工具结果出现 traceback/error/exit_code=1 时，planner 从失败证据抽取文件路径并下发 `Read` 做下一轮诊断。
- ✅ 修复闭环继续推进：
  - diagnostic `Read` 后，`prepare_upstream_body()` 会把失败和源码 evidence 注入上游；
  - prompt 明确要求 chat-only upstream 只有在证据能证明安全补丁时，输出单个 JSON `Edit` 工具请求；
  - gateway 会把该结构化 patch JSON 重新适配为下游声明的 `Edit(file_path, old_string, new_string)` schema，再交给客户端执行。
- ✅ QA repeat loop 已有关键验证环：
  - `Edit` / `Write` 工具结果回传后，如果原始任务包含测试/构建/修复意图，planner 会自动下发验证命令；
  - 验证命令仍由下游执行，按 workspace manifest 自动选择 `pytest` / `go test` / `npm test`。
  - 验证通过后不再重复下发工具，会把 pass evidence 注入 chat-only upstream 做最终 synthesis。
- ✅ evidence compaction 从纯截断升级为“周期性 LLM 摘要优先、失败后 rolling extractive fallback”：
  - 默认每 4 个新工具结果或 summary 超过阈值触发；
  - 通过 `GATEWAY_AGENT_PLANNER_LLM_SUMMARY=off` 可关闭；
  - 通过 `GATEWAY_AGENT_PLANNER_SUMMARY_EVERY`、`GATEWAY_AGENT_PLANNER_SUMMARY_TRIGGER_CHARS`、`GATEWAY_AGENT_PLANNER_SUMMARY_MAX_CHARS` 调整节奏。
- ✅ 新增测试覆盖：
  - planner 首轮 Skill step；
  - Skill tool_result 后继续 Bash/结构收集；
  - 结构证据后注入 compact evidence 给上游 synthesis；
  - web/custom function 由 planner 直接下发；
  - code_search 自动补 MCP project；
  - run tests 走声明过的 shell/exec_command；
  - explicit edit 走声明过的 Edit；
  - failed test result 后进入 diagnostic Read；
  - diagnostic read 后，上游结构化 patch JSON 会转成声明过的 Edit，且参数保持 `file_path` schema；
  - Edit result 后自动 rerun tests；
  - rerun tests 通过后进入 final synthesis，不重复工具循环；
  - LLM evidence summary 触发逻辑；
  - 保持非项目类 tool_result 不重复触发工具，避免无限循环。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

仍未完成：

- streaming 路径已接入 planner direct/evidence 注入；仍需增加更完整的 streaming 多轮验收用例。
- planner workflow 已覆盖项目分析 + 常见用户机器工具 + web/custom function + code_search + test/build + explicit edit/write + 失败后诊断读取 + 结构化 patch->Edit + Edit 后自动验证 + 验证通过后的最终 synthesis；仍需真实客户端多轮执行验证。
- evidence compaction 已可接入 `_summarize_via_llm`；仍需真实长上下文压测。
- MCP/codebase-memory 调用只在下游显式声明相关工具时触发；project 参数已支持环境变量和 workspace root 自动推断，仍需真实客户端长链路验收。

### 2026-06-26 Agent Planner 多轮修复 smoke 校正

针对“真实支持 tool 的 API 与当前 adapter 差距很大”的对比，本轮把验收从“能返回工具 JSON”推进到“外层 planner 先收集足够证据，再让 chat-only upstream 合成/产出补丁”。

已修正：

- 失败测试输出里的 `site-packages` / Python runtime warning 路径不再作为诊断源码读取目标，避免 planner 被噪声 traceback 带偏。
- diagnostic `Read` 的 tool 输入参数会进入 evidence summary，例如 `file_path=tests/test_app.py`，让上游看到证据来源。
- 读取到带行号源码（如 `1: from src.app import ok`）时，planner 会解析 import，并继续下发 `Read(src/app.py)`，而不是让弱上游凭测试文件猜补丁。
- chat-only upstream 仍只做两件事：在完整 evidence 后输出结构化 `Edit` JSON；验证通过后生成最终用户总结。

新增验证：

```bash
python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true
# steps=["Bash", "Read", "Read", "Read", "Read", "Edit", "Bash"]
# upstream_calls=2

python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py src/gateway_http_handler.py tests/integration/agent_planner_multiround_smoke.py

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

当前结论：差距确实不是一般大；正确方向不是继续增强“文本 tool-call 解析”，而是让 gateway 成为 Agent Planner：先计划、调工具、读证据、压缩上下文、验证闭环，最后才调用弱上游做语言合成。

### 2026-06-26 Agent Planner session 与无限上下文补强

本轮继续按“Gateway 外层 Agent Planner，而不是普通 gateway shim”的方向补核心能力：

- ✅ planner session key 从“最后一条 user 消息”改为“首个真实用户请求/显式 metadata”，避免多轮 tool_result 回传时 session 漂移。
- ✅ metadata 支持 JSON 字符串形式的 `user_id` / `session_id` / `conversation_id`，兼容 Codex/Claude Code 可能嵌套的会话标识。
- ✅ 上游上下文有限时，执行顺序改为：
  1. 基于完整原始请求记录 planner evidence 到 `.gateway_runtime/agent_planner.sqlite3`；
  2. 运行全局 context compaction / fanout 逻辑压缩传输 payload；
  3. 最后把 planner compact evidence summary 注入压缩后的上游请求。
- ✅ streaming 与 non-streaming 都采用同样顺序，避免“先注入 evidence 后又被 context compaction 抹掉”。
- ✅ 新增回归测试证明：
  - tool_result 多轮不会改变 planner session key；
  - 即使 `context.max_input_tokens` 很小并触发 compaction，上游请求仍包含 `Gateway Agent Planner evidence` 和关键文件证据。

新增验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_stays_stable_across_tool_result_turns \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction -q
# 2 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

当前意义：无限上下文不只是 `gateway_context.py` 对原始消息压缩；Agent Planner 自己也必须有稳定 session、持久 evidence summary，并且在任何上游窗口限制后重新注入“可用证据”。这一步已补上。

### 2026-06-26 Agent Planner 显式计划/进度工具调度

继续把系统从 gateway shim 放大为外层智能 Agent Planner：现在 planner 不只负责选择 Bash/Read/Skill/Edit，也会在下游显式声明计划工具时，先发布任务计划。

新增能力：

- ✅ 当下游声明 `update_plan` 或 `TodoWrite`，且任务是项目分析、测试修复、代码搜索、编辑等多步 workflow 时，planner 会先下发 `planner_progress` 工具调用。
- ✅ project analysis 场景中，计划步骤会先于 `Skill(codebase-onboarding)` / Bash / Read，模拟原生 Agent 的 todo/progress 行为。
- ✅ `update_plan` 工具结果回传后，planner 不会循环重复计划，而是继续进入真实执行链路，例如 `Skill(codebase-onboarding)`。
- ✅ 该能力只在下游显式声明 `update_plan`/`TodoWrite` 时启用；未声明时不会乱发未知工具，保持兼容现有客户端。

新增验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_emits_update_plan_before_project_tools_when_declared \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_to_skill_after_update_plan_result -q
# 2 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

当前意义：Agent Planner 开始具备“先计划、再执行、再综合”的外层 agent 行为；chat-only upstream 仍只负责最后语言表达。

### 2026-06-26 Agent Planner codebase-memory 项目分析优先级

继续补齐“外层 Agent Planner”而不是普通 Bash fallback：项目分析 workflow 现在优先使用下游声明的 codebase-memory/MCP 图谱能力。

新增能力：

- ✅ `project_analysis` 结构收集优先级调整为：
  1. `mcp__codebase_memory_mcp__get_architecture` / `get_architecture`
  2. `mcp__codebase_memory_mcp__search_graph` / `search_graph`
  3. `mcp__codebase_memory_mcp__search_code` / `search_code`
  4. `LS` / `Glob`
  5. `Bash` fallback
- ✅ `search_graph/search_code` 自动补 `project` 参数，沿用 `GATEWAY_CODEBASE_MEMORY_PROJECT` / workspace root 推断。
- ✅ Skill onboarding 后，如果下游同时声明 `search_graph` 和 `Bash`，planner 会优先下发 `search_graph`，避免过早退到 shell 文件枚举。

新增验证：

```bash
python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_codebase_search_graph_for_project_structure -q
# 1 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests   tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream   tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool   tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool   tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

当前意义：`分析这套项目` 不再只是 Skill/Bash 文件枚举；当客户端暴露代码图谱工具时，外层 planner 会优先走 codebase-memory 图谱收集证据，更接近原生 coding agent 的项目理解流程。

### 2026-06-26 Agent Planner streaming evidence/compaction 验收

继续补前面标记的 streaming parity 缺口：streaming 路径现在不只验证 direct tool request，也验证了“tool_result -> planner evidence -> context compaction -> upstream final SSE”的链路。

本轮修复/验证：

- ✅ 修复 `src/gateway_streaming.py::_run_streaming_orchestration_scoped` 中 `_agent_prepare_upstream_body` 只在外层函数局部 import 的问题；非 direct-response streaming 路径此前会触发 `NameError`。
- ✅ 新增 streaming 回归：当请求包含 tool_result 且上游 context 很小时，streaming orchestration 会：
  1. 先把完整 evidence 写入 planner state；
  2. 触发 context compaction；
  3. 再向压缩后的上游 payload 注入 `Gateway Agent Planner evidence`；
  4. 最终正常输出 SSE `message_stop`。
- ✅ 证明 streaming 和 non-streaming 在 planner evidence/context compaction 顺序上保持一致。

新增验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction -q
# 1 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed
```

当前意义：streaming 路径不再只是旁路 direct tool request；它也进入了外层 Agent Planner 的 evidence/无限上下文契约。

### 2026-06-26 Agent Planner core-flow trace 阶段

继续把 gateway 从 tool-call shim 放大为外层 Agent Planner：项目分析 workflow 现在不会在“列目录/拿架构概览”后立刻交给 chat-only upstream 总结，而是记录 planner-managed step，并在结构证据返回后追加一个 `core_flow_trace` 阶段。

新增能力：

- ✅ planner 会从 `planner_<step>_<uuid>` 工具调用 id 中恢复已完成步骤，写入 `completed_steps`，让多轮 tool_result 能驱动状态机继续前进。
- ✅ `project_structure` 完成后，若下游声明 codebase-memory 工具，优先继续调用 `search_graph` 查询核心请求流、入口、路由、handler、tool execution 链路。
- ✅ 如果没有 code graph 工具，则退到 `search_code`，再退到安全的 Bash grep；仍然只调用下游声明过的工具。
- ✅ 这一步让 `分析这套项目` 更接近原生 coding agent：Skill/MCP/架构概览之后继续追核心流程，而不是只靠文件列表生成泛泛总结。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py tests/test_gateway.py

python3 -m pytest \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_records_completed_steps_from_planner_tool_ids -q
# 2 passed

python3 -m pytest \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 34 passed

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; steps=["Bash","Read","Read","Read","Read","Edit","Bash"]; upstream_calls=2
```

当前意义：planner 开始显式维护 workflow step，而不是只靠“出现过哪些工具名”猜下一步；这是后续扩展完整 workflow graph、trace_path/get_code_snippet、长链路 smoke 的基础。

### 2026-06-26 Agent Planner symbol deep-dive 阶段

继续把 `分析这套项目` 从“结构/流程搜索”推进到“符号级实现证据”。现在 planner 在 `core_flow_trace` 返回 code graph 结果后，会解析 `qualified_name`，并优先下发 codebase-memory 的源码/调用链工具，而不是让 chat-only upstream 根据搜索摘要猜实现。

新增能力：

- ✅ 从 code graph/search evidence 中提取 `qualified_name`。
- ✅ `core_flow_trace` 完成后新增 `symbol_deep_dive` 阶段。
- ✅ 若下游声明 `mcp__codebase_memory_mcp__get_code_snippet`，自动补 `project` 并请求核心符号源码。
- ✅ 若下游声明 `mcp__codebase_memory_mcp__trace_path`，自动以 qualified name 的 leaf function 追双向调用链。
- ✅ 仍然遵守 caller-declared schema：未声明工具不下发；`project`、`qualified_name`、`function_name` 参数按下游 schema 适配。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py tests/test_gateway.py

python3 -m pytest \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace -q
# 1 passed

python3 -m pytest \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 35 passed

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2
```

当前意义：项目分析 workflow 已从 `Skill -> structure -> core_flow` 继续推进到 `symbol_deep_dive`，chat-only 模型最终看到的是经过 planner 选择、压缩、归因的源码/调用链证据。

### 2026-06-26 Streaming 多轮 Agent Planner 闭环验收

继续补之前标记的 streaming 缺口：现在已新增测试证明 streaming 路径不只是能把单个 direct tool request 包成 SSE，也能跨多轮 tool_use/tool_result 继续由外层 planner 调度下一步。

新增验收覆盖：

```text
stream request 1: user=分析这套项目
  -> SSE tool_use Skill(codebase-onboarding)

stream request 2: Skill tool_result 回传
  -> SSE tool_use mcp__codebase_memory_mcp__search_graph(project architecture)

stream request 3: project_structure tool_result 回传
  -> SSE tool_use mcp__codebase_memory_mcp__search_graph(core request flow)
```

关键意义：

- ✅ streaming 路径也遵守“chat-only upstream 不负责规划”的契约。
- ✅ 多轮状态由 gateway planner/tool evidence 驱动，不依赖 upstream 自己记住下一步。
- ✅ 工具结果回来后，streaming 仍会继续 Skill -> code graph -> core flow 的 workflow，而不是直接让弱上游输出泛泛总结。
- ✅ 与已有 streaming evidence/context compaction 测试互补：一个验证“多轮继续调度”，一个验证“最终综合前证据不被压缩吃掉”。

验证：

```bash
python3 -m pytest \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools -q
# 1 passed

python3 -m pytest \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_skill_read_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction -q
# 5 passed
```

### 2026-06-26 Streaming symbol deep-dive -> final synthesis 验收

继续补 streaming 完整闭环：现在新增测试证明，当 streaming 多轮已经完成 `project_structure -> core_flow_trace -> symbol_deep_dive` 后，planner 不再继续发工具，也不会让 chat-only upstream 猜上下文；而是先把完整 tool evidence 写入 planner state，触发 context compaction，再把 compact planner evidence 重新注入上游请求，最后将 upstream 的最终回答包装成 SSE 返回。

新增验收链路：

```text
stream request with completed planner steps:
  planner_project_structure_* tool_result
  planner_core_flow_trace_* tool_result(含 qualified_name)
  planner_symbol_deep_dive_* tool_result(源码片段)

Gateway streaming orchestration:
  persist full planner evidence
  -> compact oversized upstream payload
  -> inject Gateway Agent Planner evidence
  -> call chat-only upstream once for final synthesis
  -> stream final SSE message_stop
```

验证点：

- ✅ 上游请求包含 `Gateway Agent Planner evidence`。
- ✅ 上游请求仍保留 `run_tool_orchestration` / `symbol_deep_dive` 关键证据。
- ✅ context window 很小时仍有 `gateway_context.compacted=true`。
- ✅ 下游收到最终 SSE 文本与 `message_stop`。
- ✅ 不再输出新的 `tool_use` stop_reason。

验证命令：

```bash
python3 -m pytest \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 1 passed

python3 -m pytest \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction -q
# 3 passed
```

当前意义：streaming 下已经覆盖“继续调工具”和“工具链完成后最终综合”两条关键路径，和 non-streaming 的 planner/evidence/context 契约进一步对齐。

### 2026-06-26 真实 Claude/Codex CLI smoke 与 one-shot 工具循环修复

本轮从 mock/integration 进入真实 CLI 验证，运行 `project_scope_cli_smoke.py --require-claude --require-codex`，发现并修复两个真实客户端问题：

1. **本机代理影响 smoke 自检**
   - 症状：脚本自身用 `urllib` 请求 `127.0.0.1/healthz` 时被全局代理干扰，表现为 `Remote end closed connection without response`。
   - 修复：脚本入口设置 `NO_PROXY/no_proxy=127.0.0.1,localhost`，确保 smoke 真连本地 Gateway。

2. **Codex Responses one-shot read 循环**
   - 症状：Codex 已执行 `exec_command` 读取文件并回传 `function_call_output`，但 planner 仍根据原始 user prompt 反复下发相同 read/shell 工具，导致 CLI 卡到超时。
   - 根因：generic one-shot 工具意图没有在 evidence 已存在时停止重复调度。
   - 修复：`_generic_intent_decision()` 对 explicit Skill / shell / read / list 这类一次性工具请求，在已有 tool evidence 后返回 `None`，进入上游 final synthesis。
   - 同时补 Responses streaming `response.completed.usage.total_tokens`，修复 Codex 解析 `ResponseCompleted` 时缺字段的问题。

新增/相关验证：

```bash
python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true
# claude.ok=true
# codex.ok=true

python3 -m pytest \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_repeat_read_after_tool_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_repeat_responses_read_after_function_output -q
# 2 passed

python3 -m pytest \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_responses_has_item_before_text_delta \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 39 passed
```

当前意义：真实 Claude CLI 与真实 Codex CLI 都已通过本地 Gateway + chat-only mock upstream 的项目作用域/工具回传 smoke；并且修复了 Codex Responses 协议下会无限重复一次性 read 工具的真实问题。

### 2026-06-26 完整 project_analysis 长链路 smoke

本轮补上真正的项目分析 workflow 集成 smoke，不再只验证单点规则。新增 `tests/integration/agent_planner_project_analysis_smoke.py`，模拟下游客户端执行 planner 发出的原生工具，并使用 chat-only fake upstream 做最终综合。

覆盖链路：

```text
user: 分析这套项目
  -> update_plan
  -> Skill(codebase-onboarding)
  -> mcp__codebase_memory_mcp__search_graph(project architecture)
  -> mcp__codebase_memory_mcp__search_graph(core request flow)
  -> mcp__codebase_memory_mcp__get_code_snippet(qualified_name)
  -> mcp__codebase_memory_mcp__trace_path(function_name)
  -> Read(key files)
  -> chat-only upstream final synthesis
```

同时修复一个证据注入边界：当 Gateway recalled memory 在消息前插入额外 user 消息、且 Anthropic messages 中存在多个 system 消息时，planner evidence system prompt 可能在 Anthropic->OpenAI Chat 转换时被后续 system 覆盖。现在：

- `planner_session_key` 跳过 `[Gateway recalled memory]` / planner evidence 这类注入噪声，保持 session anchor 稳定。
- `_convert_anthropic_messages_to_openai()` 会合并多个 system 消息，而不是后一个覆盖前一个，确保 `Gateway Agent Planner evidence` 不丢。

验证：

```bash
python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true
# steps=["update_plan","Skill","mcp__codebase_memory_mcp__search_graph","mcp__codebase_memory_mcp__search_graph","mcp__codebase_memory_mcp__get_code_snippet","mcp__codebase_memory_mcp__trace_path","Read","Read"]
# upstream_calls=1

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_responses_has_item_before_text_delta \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::NativeGatewayTests::test_default_config_and_admin_post_save_upstream_capabilities -q
# 39 passed
```

当前意义：项目分析已经具备完整外层 planner 证据链：计划、技能、代码图谱、核心流程、源码/调用链、关键文件、证据注入、最终综合。chat-only upstream 只在最后收到压缩证据并输出用户可读分析。

### 2026-06-26 回归收口：保留 Gateway-owned 工具与普通 fanout 路径

用户对比真实 tool-capable API 后指出差距明显，本轮继续按“外层 Agent Planner 不是简单 tool-call shim”处理，并修复全量回归中暴露的两个边界：

1. **Gateway-owned HTTP Action 不应被 planner 当作下游 custom function 抢走**
   - 症状：`get_weather` 同时是请求声明 tool 与 Gateway HTTP Action 时，planner 先返回 protocol-level tool_call，导致 Gateway 没有执行 HTTP Action，也没有把结果 round-trip 给 upstream。
   - 修复：`src/gateway_agent_planner.py::_custom_function_tool_call()` 跳过 Gateway-owned HTTP Action / MCP tool，让它们继续走 Gateway 本地执行链路。

2. **无 tools 的普通大上下文请求不应被 project_analysis planner 抢先返回假工具**
   - 症状：普通 `/v1/chat/completions` 无 tools 请求中包含“分析这套项目”且上游返回 too-long 时，旧的 context fanout 应接管；planner 却先构造 `Glob/LS` 类 synthetic tool_call，导致 final 为 `None`。
   - 修复：`plan_downstream_tool_request()` 对 project_analysis 要求请求体存在声明工具；没有 downstream tool surface 时回到原 upstream/context fanout 路径，不再发明客户端工具。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::NativeGatewayTests::test_upstream_too_long_response_triggers_forced_fanout -q
# 2 passed

python3 -m pytest -q
# 923 passed, 2 skipped

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1; steps=["update_plan","Skill","mcp__codebase_memory_mcp__search_graph","mcp__codebase_memory_mcp__search_graph","mcp__codebase_memory_mcp__get_code_snippet","mcp__codebase_memory_mcp__trace_path","Read","Read"]

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2; steps=["Bash","Read","Read","Read","Read","Edit","Bash"]

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true

git diff --check
# clean

grep -RIn --exclude-dir=.git --exclude-dir=.gateway_runtime --exclude='.gateway_service.json' --exclude='.case.txt' '<redacted-real-key>' . 2>/dev/null || true
# clean
```

当前结论：差距确实不小，关键不是格式兼容，而是外层 planner 必须清楚区分 **downstream-owned user-machine tools**、**Gateway-owned HTTP/MCP tools**、以及 **plain no-tools chat/context fanout**。这三个边界已补回，并通过全量回归。

### 2026-06-26 无限上下文增强：普通历史也进入 summary + recent 压缩

本轮继续把 Gateway 往外层 Agent Planner 推进，重点补“上游上下文有限时，隔一段时间总结一下”的非工具历史路径。之前 planner evidence 已有周期压缩，但普通 chat/messages/responses 历史在 `_compact_request_for_upstream()` 中主要是截断旧消息；这会让 chat-only upstream 在长会话里出现硬性失忆。

已改造：

- `src/gateway_context.py::_compact_messages_with_summary()`
  - LLM 摘要可用时保留 `[Previous conversation summary]`。
  - LLM 摘要不可用时，不再只放占位符，而是生成 bounded extractive digest：按 role 保留旧消息摘要。
- `src/gateway_context.py::_compact_request_for_upstream()`
  - `/v1/chat/completions`：system prompt = gateway compaction prompt + previous conversation summary，后面保留 recent messages。
  - `/v1/messages`：不向 Anthropic messages 注入 `role=system` 消息，而是把摘要合并进独立 `system` 字段，并对原始 system 做长度裁剪。
  - `/v1/responses`：`input` 为 message list 时同样使用 summary + recent，而不是只 trim。

新增回归：

- `ContextSummarizationTests::test_compact_request_for_upstream_injects_periodic_summary`
- `ContextSummarizationTests::test_messages_compaction_moves_summary_into_system_field`
- `ContextSummarizationTests::test_responses_input_list_compaction_keeps_summary_and_recent_items`

验证：

```bash
python3 -m pytest tests/test_gateway.py::ContextSummarizationTests \
  tests/test_gateway.py::NativeGatewayTests::test_text_tool_adapter_compacts_huge_claude_code_payload_before_upstream -q
# 8 passed

python3 -m pytest -q
# 926 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

当前意义：现在“无限上下文”不只覆盖 planner tool evidence，也覆盖普通长对话历史；当上游窗口不足时，旧上下文会以 summary/digest 形式继续注入，chat-only 模型只负责基于压缩后的证据和最近消息做最终表达。

### 2026-06-26 Gateway-owned 工具预执行：上游不再负责发明 HTTP Action 调用

本轮继续收敛到“chat-only upstream 只做最终表达，外层 Agent Planner 负责功能调度”。之前 Gateway-owned HTTP Action 仍主要依赖弱上游先输出 XML/function-call 文本，再由 Gateway 解析执行；这仍然让 chat-only 模型承担了工具选择职责。

已改造：

- `src/gateway_tool_runtime.py`
  - 新增 `_gateway_owned_tool_call_from_user_text()`：从用户意图和声明工具中识别明显匹配的 Gateway-owned HTTP Action / MCP connector。
  - 新增 `_preexecute_gateway_owned_planner_tool()`：在调用 upstream 前执行 Gateway-owned 工具，把 tool result 追加到对话，再让 upstream 只做 final synthesis。
  - `_run_tool_orchestration_scoped()` 在 weak upstream adapter 模式下先运行该 planner preexecute 路径。
  - preexecute 完成后移除 `tools/tool_choice`，避免 chat-only upstream 再收到 native tool schemas 或 text adapter 工具手册。

回归更新：

- `NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips`
  - 旧路径：upstream 第一次输出 `<function=get_weather>...`，Gateway 再执行 HTTP Action，第二次 upstream final。
  - 新路径：Planner 先执行 `get_weather(city=Shanghai)`，upstream 只调用一次并收到 `temp_c` 工具结果做最终回答。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_exposes_schema_and_executes_real_http \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_http_error_records_tool_failure \
  tests/test_gateway.py::NativeGatewayTests::test_http_action_response_max_bytes_is_enforced -q
# 4 passed

python3 -m pytest -q
# 926 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

当前意义：Gateway-owned 服务能力已经从“弱上游文本工具适配”前移到“外层 planner 预执行”。这更符合目标架构：HTTP/MCP 等 Gateway service-side 功能由 agent planner 决策和执行，chat-only 上游只基于结果生成对话。

### 2026-06-26 Streaming 对齐：Gateway-owned 预执行也覆盖流式请求

上轮已经把 non-streaming HTTP Action 从“上游先发明工具调用”迁到“planner 预执行后让上游最终表达”。本轮补齐 streaming 路径，避免 stream 模式仍退回弱上游猜工具。

已改造：

- `src/gateway_streaming.py::_run_streaming_orchestration_scoped()`
  - weak upstream adapter 模式下，在进入 upstream streaming synthesis 前调用 `_preexecute_gateway_owned_planner_tool()`。
  - 与 non-streaming 一致：Gateway-owned HTTP/MCP tool 先执行，tool result 注入上下文，上游只收到结果并输出最终流式文本。

新增回归：

- `AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream`
  - 配置 `get_weather` HTTP Action。
  - 调用 streaming `/v1/chat/completions`。
  - 断言 HTTP server 先收到 `city=Shanghai`。
  - 断言 upstream 只收到一次请求，且请求里已有 `temp_c` tool result，没有 `tools`。
  - 断言 SSE 输出最终文本并无 error event。

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_evidence_survives_context_compaction -q
# 4 passed

python3 -m pytest -q
# 927 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

当前意义：Gateway-owned service tools 的 planner 预执行能力现在覆盖 non-streaming 与 streaming 两条路径。chat-only upstream 在两种模式下都更接近“只做 synthesis”。

### 2026-06-26 Service-side registry：HTTP Action 不再依赖客户端显式 tools 声明

本轮继续把 Gateway 从“客户端声明什么工具就转发什么”推进到“外层 Agent Planner 维护服务侧能力注册表”。之前 Gateway-owned preexecute 仍主要从请求 `tools` 中识别 HTTP Action；如果客户端发普通 chat 请求但 Gateway 已配置 HTTP Action，planner 不会主动使用该服务侧能力。

已改造：

- `src/gateway_tool_runtime.py::_gateway_owned_tool_call_from_user_text()`
  - 除 caller-declared tools 外，也读取 `_enabled_http_actions()`。
  - 配置中的 HTTP Action 成为 planner 可发现的 service-side capability。
  - 明显匹配用户意图时，即使请求没有 `tools`，也会先执行 HTTP Action，再把结果交给 chat-only upstream synthesis。
- sync 与 streaming 共用 `_preexecute_gateway_owned_planner_tool()`，因此两条路径同时支持。

回归更新：

- `NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips`
  - 请求体不再带 `tools`。
  - Gateway 从配置识别 `get_weather`，执行 `city=Shanghai`，upstream 只收到一次 final synthesis 请求。
- `AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream`
  - streaming 请求体同样不带 `tools`。
  - 断言 SSE 路径也由 planner 预执行 HTTP Action。

边界验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_upstream_too_long_response_triggers_forced_fanout \
  tests/test_gateway.py::ToolCallDefaultTests::test_text_tool_adapter_keeps_plain_chat_plain_without_tool_intent \
  tests/test_gateway.py::ToolCallDefaultTests::test_text_tool_adapter_strips_tools_and_injects_prompt -q
# 3 passed
```

完整验证：

```bash
python3 -m pytest -q
# 927 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

当前意义：Planner 不再只依赖客户端请求中显式声明的工具；Gateway 已配置的 service-side HTTP Action 已进入 planner registry。这更接近“agent 提供意图解析和功能调度”，而不是单纯 gateway/proxy。

### 2026-06-26 MCP service-side registry：配置型 MCP 工具也可无 tools 声明预执行

本轮继续把 Gateway 从 proxy/shim 推到 Agent Planner runtime：HTTP Action 已经进入 service-side registry，本轮把 configured MCP connector 也接入 planner preexecute。目标是：客户端不必每次把 MCP tool schema 放进请求，Gateway 自己配置的 MCP server/tool 也属于 planner 的能力注册表。

已改造：

- `src/gateway_tool_runtime.py::_gateway_owned_tool_call_from_user_text()`
  - 除 caller-declared MCP tools 外，也读取 `_enabled_mcp_servers()`。
  - 通过 `_mcp_list_server_tools()` 发现 configured MCP tools。
  - 使用 `mcp__server__tool` public name 加入 planner scoring。
  - 命中用户意图后，planner 预执行 MCP tool，注入结果，再让 chat-only upstream final synthesis。
  - MCP discovery 在 preexecute 中是 best-effort；失败时不阻断普通请求，显式 MCP tool path 仍保留详细错误。

新增/更新回归：

- `NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools`
  - 启动真实 stdio fake MCP server。
  - 请求体不带 `tools`。
  - Planner 从配置发现 `echo_mcp`，执行后把 `mcp:Echo via MCP value ok` 注入 upstream 请求。
- `AnthropicSSEFormatTests::test_streaming_configured_mcp_tool_preexecutes_without_request_tools`
  - streaming 路径同样验证无 `tools` 声明的 configured MCP preexecute。
- 同时修复测试隔离：新增 MCP preexecute 测试结束后清理 `MCP_TOOL_CATALOG_CACHE`，避免污染既有 MCP catalog cache 断言。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_mcp_stdio_tools_list_call_and_schema_merge \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_configured_mcp_tool_preexecutes_without_request_tools -q
# 3 passed

python3 -m pytest -q
# 929 passed, 2 skipped, 21 warnings

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true
```

当前意义：Planner 的 service-side registry 现在覆盖 HTTP Action 与 MCP connector。Gateway 配置本身就是 Agent runtime 的工具注册表，chat-only upstream 继续只做最终表达。

### 2026-06-26 Planner observability：最终响应携带 gateway_context.agent_planner

用户对比真实 tool API 后指出差距很大，本轮继续补“Agent runtime 可观测性”：不能只在发给上游的内部请求里看到 planner 做了什么，最终返回给下游/调试端的响应也必须带出 runtime 元数据。

已改造：

- `src/gateway_tool_runtime.py::_attach_request_gateway_context()`
  - 将 synthesis 请求中的 `gateway_context` 白名单字段复制到最终响应：`agent_planner`、`local_planner`、`planner_evidence_chars`、`compacted`、`strategy`。
  - 解决 Gateway-owned preexecute 后最终响应看起来像普通 upstream response、无法判断走了哪个 planner workflow 的问题。
- `src/gateway_tool_runtime.py::_run_tool_orchestration_scoped()`
  - non-streaming final synthesis 返回前附加 planner/runtime metadata。
- `src/gateway_streaming.py::_run_streaming_orchestration_scoped()`
  - streaming final synthesis 分支同样附加 metadata；本轮先修复缺失导入导致 SSE error 的回归。
- `tests/test_gateway.py`
  - HTTP Action 与 configured MCP 的 non-streaming 回归现在断言最终响应也包含 `gateway_context.agent_planner.workflow == "gateway_owned_tool"`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools -q
# 2 passed

python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_configured_mcp_tool_preexecutes_without_request_tools -q
# 2 passed
```

当前意义：这不是原生模型 tool-call 能力本身，但它是外层 Agent Planner 必需的运行时观测面。现在能从最终响应判断本轮是否由 Gateway planner 执行了 service-side tool，以及执行的是哪类 workflow。

### 2026-06-26 Built-in service tools 进入 Planner registry：calculator/current_time/WebSearch

继续沿着“Gateway 不只是协议 shim，而是外层 Agent Planner runtime”的方向推进：HTTP Action 与 MCP 已经可以不依赖客户端 `tools` 声明由 planner 预执行，本轮把安全的 Gateway built-in service tools 也纳入 service-side capability registry。

已改造：

- `src/gateway_tool_runtime.py::_gateway_owned_tool_call_from_user_text()`
  - 增加内置能力发现：`calculator`、`current_time`、`WebSearch`。
  - 仅纳入非 user-machine 风险的能力；Read/Write/Bash/GUI/local agent 等仍默认 surface 给下游 Claude/Codex 执行。
  - 明显计算意图（如 `Calculate 6*7`）会在 upstream 前执行 `calculator`，把结果注入 synthesis 请求。
  - 明显时间/搜索意图同样进入 service-side registry；搜索仍受原有 web-search intent 判断约束。
- `tests/test_gateway.py`
  - 新增 non-streaming built-in calculator preexecute 回归。
  - 新增 streaming built-in calculator preexecute 回归。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_builtin_calculator_preexecutes_without_request_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_weather_http_action_executes_and_roundtrips \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_http_action_preexecutes_before_upstream -q
# 4 passed

python3 -m pytest tests/test_gateway.py::ToolCallDefaultTests::test_text_tool_adapter_keeps_plain_chat_plain_without_tool_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_web_search_uses_declared_downstream_tool_name \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_repeat_read_after_tool_result \
  tests/test_gateway.py::NativeGatewayTests::test_configured_mcp_tool_preexecutes_without_request_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_configured_mcp_tool_preexecutes_without_request_tools -q
# 5 passed
```

当前意义：Planner registry 现在不仅覆盖配置型 HTTP Action/MCP，也覆盖 Gateway 自带的 service tools。chat-only upstream 对这类请求只看工具结果并负责语言表达；工具选择和执行继续前移到外层 Agent Planner。

### 2026-06-26 无限上下文增强：Conversation Memory 周期 rollup

继续补“上游上下文有限，但 Gateway/Agent Planner 负责无限上下文”的能力。之前已有：请求超限时做 previous summary + recent messages、每轮 conversation memory 入 SQLite、planner evidence 周期压缩。本轮新增 **会话级周期 rollup**：即使请求暂时没触发 token 超限，也会每隔 N 轮把历史 turn summary 合并成长期摘要，并在后续请求中优先召回。

已改造：

- `src/gateway_context.py`
  - 新增 `memory_rollup_every_turns` / `GATEWAY_MEMORY_ROLLUP_EVERY_TURNS`，默认 8。
  - 新增 `memory_rollup_max_chars` / `GATEWAY_MEMORY_ROLLUP_MAX_CHARS`，默认 4000。
  - `_remember_conversation_turn()` 每写入一轮 compact memory 后检查是否达到 rollup 周期。
  - 达到周期后写入 `kind=session_rollup` 的 `[Periodic conversation summary]`。
  - `_recall_conversation_memories()` 会把最新 `session_rollup` 前置注入，确保长期摘要不会被普通关键词检索挤掉。
  - 可选 `GATEWAY_MEMORY_ROLLUP_LLM_SUMMARY=1` 时用上游 LLM 做 rollup summary；否则使用 extractive fallback，保证上游不可用时仍然不失忆。
- `src/gateway_config.py` / `gateway.config.json`
  - 增加 rollup 默认配置。
- `tests/test_gateway.py`
  - 新增 `test_conversation_memory_periodic_rollup_is_recalled`：两轮记忆后生成 session rollup，第三轮请求会召回并注入 alpha/beta 历史摘要。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_recalls_same_session_workspace_only \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_compacts_huge_turns_in_sqlite -q
# 3 passed
```

当前意义：无限上下文不再只依赖“超限后临时压缩”。Gateway 现在会持续维护长期会话 rollup，后续请求先看到周期摘要，再由 chat-only upstream 做表达。

### 2026-06-26 远端服务边界复核：Planner 必须多租户隔离，不能是本地增强服务

用户明确要求暂停反思：这套系统是远端服务形态，不是把 Gateway 服务机当本地 Claude/Codex 的增强工具。基于这个前提，本轮重点修正多用户/多 workspace 隔离边界。

已改造：

- `src/gateway_tool_runtime.py::_create_anonymous_workspace()`
  - 不再用 `model + first user message` 生成匿名 workspace。远端服务中两个用户可能发完全相同 prompt，不能共享目录。
  - 无 client workspace / session identity 时改为每请求随机隔离目录。
  - 有 session identity 时使用 `tenant/user + session` 生成 hash 化目录名，避免路径穿越和用户间 session id 碰撞。
- `src/gateway_agent_planner.py::planner_session_key()`
  - Planner session key 增加 tenant/user 维度：同 workspace、同 `session_id`，不同用户也不会共享 planner evidence/workflow state。
  - `AgentPlannerStore` 增加 `RLock`、SQLite `busy_timeout`、WAL 初始化，提升并发远端请求下的稳定性。
- `src/gateway_context.py::_memory_session_key()`
  - conversation memory session key 增加 tenant/user 维度。
  - 无 session 且匿名用户时不再用 prompt hash 作为稳定 session，避免跨用户记忆污染。
- `tests/test_gateway.py`
  - 新增匿名相同 prompt 不共享 workspace 回归。
  - 新增同 session 不同用户 workspace 隔离回归。
  - 新增 planner session key tenant 隔离回归。
  - 新增并发 direct tool calls 各自读取 client workspace 的回归。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_parallel_direct_tool_calls_keep_client_workspaces_isolated \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_not_shared_by_identical_prompts \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_tenant_session_scoped \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_is_tenant_scoped_for_remote_service -q
# 4 passed
```

当前结论：后续所有 Agent Planner 能力都必须按“远端多租户 runtime”设计：用户机器工具默认下发给 client；Gateway-owned service tools 才在服务端执行；任何 planner/memory/workspace 状态必须带 tenant/session/workspace 隔离。

### 2026-06-26 远端 runtime 内存态隔离加固：裸 session/team id 不能全局共享

继续按用户最新要求复核：Gateway 是远端多租户 Agent Planner 服务，不是本地增强服务。上一轮已隔离 planner/memory/workspace，但本轮发现 `gateway_builtin_tools.py` 仍有进程级内存态 map 使用调用方提供的裸 id：`EXEC_SESSIONS`、`AGENT_SESSIONS`、`TEAM_SESSIONS`、`PENDING_USER_QUESTIONS`。在远端服务中，两个用户完全可能同时使用 `session_id=dev` / `team_id=shared-team`，裸 id 会导致覆盖、串线或误删除。

已改造：

- `src/gateway_builtin_tools.py`
  - 新增 `_RUNTIME_SCOPE_OVERRIDE` ContextVar。
  - 新增 `_runtime_scope_key()` / `_scoped_runtime_id()`，对 long-lived runtime state 做内部命名空间隔离。
  - `exec_shell_start` / `write_stdin` / `exec_wait` / `exec_kill` 对外仍返回原始 `session_id`，内部按 `tenant + session + workspace + public_id` 存取。
  - `spawn_agent` / `send_input` / `wait_agent` / `close_agent` / `resume_agent` 同样按 runtime scope 隔离。
  - `TeamCreate` / `SendMessage` / `TeamDelete` 同样按 runtime scope 隔离；顺手修正 `SendMessage` 在持锁状态下递归调用 `send_input` 的潜在死锁风险。
- `src/gateway_tool_runtime.py`
  - `_workspace_scope(root, body)` 现在同时设置 workspace ContextVar 和 runtime-scope ContextVar。
  - runtime scope 来源：`tenant/user + session/conversation + resolved client workspace`。
  - 匿名无 session 请求使用 per-request scope，不再把裸 tool session id 放到全局命名空间里。
- `src/gateway_agent_planner.py`
  - 去掉 planner workspace/codebase 推断中的 `Path.cwd()` fallback，避免服务机 cwd 进入 planner key 或项目名推断。
- `gateway.config.json` / `gateway.config.yaml`
  - 移除默认 `gateway.workspace_root=./workspace`，避免远端部署在缺失 client workspace 时退回服务机目录；缺失 workspace 继续走 anonymous isolated workspace。
- `tests/test_gateway.py`
  - 新增 `test_remote_exec_sessions_are_scoped_by_client_workspace_and_tenant`：两个用户使用相同 `session_id=shared-shell` 也不会串到彼此 workspace 的 shell session。
  - 新增 `test_remote_team_mailboxes_are_scoped_by_client_workspace_and_tenant`：两个用户使用相同 `team_id=shared-team` 也不会互相删除/读取 mailbox。

验证：

```bash
python3 -m py_compile src/gateway_builtin_tools.py src/gateway_tool_runtime.py src/gateway_agent_planner.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_not_shared_by_identical_prompts \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_tenant_session_scoped \
  tests/test_gateway.py::NativeGatewayTests::test_parallel_direct_tool_calls_keep_client_workspaces_isolated \
  tests/test_gateway.py::NativeGatewayTests::test_remote_exec_sessions_are_scoped_by_client_workspace_and_tenant \
  tests/test_gateway.py::NativeGatewayTests::test_remote_team_mailboxes_are_scoped_by_client_workspace_and_tenant \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_is_tenant_scoped_for_remote_service -q
# 6 passed
```

当前结论：远端稳定性边界进一步收紧。现在不仅 planner/memory/workspace 是 tenant/session/workspace scoped，进程内长生命周期 runtime state 也不再按裸 id 全局共享。

补充：本轮验证中发现 Agent Planner multi-round smoke 虽然成功，但 stderr 有 `Database not initialized. Call init_persistence() first.` 的 tool cache 噪声。已在 `src/gateway_persistence.py` 中把“persistence 未初始化”的 tool cache 读写处理为静默降级到内存缓存，不再刷 error 日志，避免远端运行时把非致命 fallback 误报为错误。

### 2026-06-26 Streaming Planner observability：流式最终响应也携带 gateway_context

继续按“远端外层 Agent Planner”目标推进。上一轮非流式响应已经能携带 `gateway_context.agent_planner`，但 streaming 路径只把最终文本/tool_use 事件发给客户端；远端调试时无法从 stream 本身确认本轮是 plain chat、downstream tool request，还是 Agent Planner final synthesis。

已改造：

- `src/gateway_streaming.py`
  - 新增 `_stream_gateway_context()` / `_attach_stream_gateway_context()`。
  - `/v1/chat/completions`：在 final stop/tool_calls chunk 顶层附带 `gateway_context`。
  - `/v1/messages`：在 `message_delta` 终止事件顶层附带 `gateway_context`。
  - `/v1/responses`：在 `response.completed.response.gateway_context` 附带 metadata。
  - 不新增自定义 SSE event，避免严格 OpenAI/Anthropic SDK 因未知 event/data shape 解析失败；只把 metadata 放进已有终止/完成事件的扩展字段。
- `tests/test_gateway.py`
  - 新增 `test_stream_final_response_carries_gateway_context_metadata`，覆盖 Chat Completions / Anthropic Messages / Responses 三种流式终止形态。

验证：

```bash
python3 -m py_compile src/gateway_streaming.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_carries_gateway_context_metadata \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_has_message_start \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_stream_final_response_responses_has_item_before_text_delta \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 4 passed
```

当前意义：流式与非流式的 planner observability 已对齐。远端服务排障时，可以从最终 stream chunk 看到 Agent Planner workflow/step/evidence 等 metadata，而不是只能依赖服务端日志。

### 2026-06-26 Agent Planner state snapshot：多轮 workflow 不再只暴露单步 metadata

继续按“完整外层 Agent Planner”目标推进。上一轮已让 streaming/non-streaming 都携带 `gateway_context`，但 `agent_planner` 里主要只有 `workflow/step/reason`，对远端多轮 runtime 来说仍然偏单次 shim：客户端/日志无法直接看到已完成步骤、证据数量、证据摘要长度、压缩次数等状态。

已改造：

- `src/gateway_agent_planner.py`
  - 新增 `planner_state_snapshot(state, max_summary_chars=1200)`。
  - snapshot 包含：`workflow`、`current_step`、`completed_steps`、`evidence_count`、`evidence_summary_chars`、`compaction_count`、`llm_compaction_count`、`session_key`、`evidence_summary_preview`。
  - `prepare_upstream_body()` 在进入 chat-only upstream final synthesis 前，把 planner state snapshot 写入 `gateway_context.agent_planner.state`，并设置 `strategy=agent_planner_final_synthesis`、`planner_evidence_chars`。
- `src/gateway_tool_runtime.py`
  - `_direct_downstream_tool_request_response()` 在返回 downstream tool request 时，也把 `planner_decision.state` 转成 snapshot 放进 `gateway_context.agent_planner.state`。
- `tests/test_gateway.py`
  - 扩展 project_analysis 多轮测试：验证 `project_structure` / `core_flow_trace` 的 state snapshot。
  - 扩展 final synthesis 测试：验证 upstream request 和最终 response 都携带 `agent_planner_final_synthesis`、`state.current_step=synthesis`、证据摘要 preview。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis -q
# 3 passed
```

当前意义：Planner 现在更像远端 Agent Runtime，而不是一次性 tool shim。每轮 tool request / final synthesis 都能带上可审计 state snapshot，便于客户端侧 UI、日志、调试器或后续 planner telemetry 使用。

兼容性修正：全量回归发现 `prepare_upstream_body()` 会覆盖 Gateway-owned tool preexecute 已写入的 `agent_planner.workflow=gateway_owned_tool`。已改为仅在没有既有 `agent_planner` 时创建 final-synthesis state snapshot；已有 Gateway-owned context 保留原 workflow/tool/success 字段。

### 2026-06-26 Agent Planner admin status endpoint：远端 runtime 状态可查询

继续按“远端外层 Agent Planner”目标推进。上一轮 state snapshot 已随响应传播，但服务端仍缺少一个只读 runtime 状态 API；多用户远端部署排障时只能看日志或 sqlite 文件，不利于确认 planner sessions、workflow、current step、evidence/compaction 状态。

已改造：

- `src/gateway_agent_planner.py`
  - `AgentPlannerStore.list_recent(limit=50)`：从 SQLite `planner_sessions` 读取最近 sessions。
  - 返回 bounded `planner_state_snapshot()`，不暴露完整长 evidence。
  - `limit` 限制在 1..500，避免一次性 dump 过大。
- `src/gateway_http_handler.py`
  - 新增只读 admin endpoint：`GET /admin/agent-planner.json?limit=50`。
  - 需要 admin Basic Auth。
  - 返回：`{"sessions": [...]}`。
- `tests/test_gateway.py`
  - 新增 `test_admin_agent_planner_endpoint_lists_runtime_sessions`：先真实触发一次 project_analysis planner tool request，再通过 HTTP admin endpoint 查询，验证 `workflow/current_step/session_key`。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_http_handler.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_mcp_health_endpoint_supports_probe_query \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result -q
# 3 passed
```

当前意义：Agent Planner 不再只有“随响应返回 metadata”，服务端也有可查询 runtime status surface。后续可以直接把这个 endpoint 接入 Admin UI，或扩展成按 tenant/session/workspace 过滤。

### 2026-06-26 Agent Planner admin filters：远端多租户状态面可按 session/tenant/workflow 检索

按用户最新要求重新校准：这不是本地增强服务，而是远端多用户 Agent Planner runtime。仅有 `/admin/agent-planner.json?limit=50` 不够；多用户并发时 admin 需要快速定位某个 tenant/session/workflow，不能靠人工翻 sqlite 或服务机日志。

已改造：

- `src/gateway_agent_planner.py`
  - `AgentPlannerStore.list_recent()` 增加只读过滤参数：`workflow`、`current_step`、`session_contains`、`tenant_contains`、`has_evidence`。
  - 有过滤时最多扫描 5000 条最近 session，再返回最多 500 条，避免远端状态接口一次性 dump 过大。
  - 仍然只返回 `planner_state_snapshot()`，不暴露完整 evidence。
- `src/gateway_http_handler.py`
  - `/admin/agent-planner.json` 支持 query filters：
    - `workflow=project_analysis`
    - `current_step=codebase_onboarding`
    - `session_contains=...`
    - `tenant_contains=...`
    - `has_evidence=1|0`
  - 返回 `{ "sessions": [...], "filters": {...}, "limit": ... }`，方便 UI/运维确认实际生效的过滤条件。
- `tests/test_gateway.py`
  - 扩展 `test_admin_agent_planner_endpoint_lists_runtime_sessions`：覆盖 admin auth、workflow/current_step/session filter、tenant filter、has_evidence filter。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_http_handler.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions -q
# 1 passed
```

当前意义：远端服务同时处理多个 client workspace / tenant / session 时，Agent Planner runtime 状态可以被安全、只读、有限量地检索；client workspace 仍由请求身份与 workspace scope 决定，不退回服务机 cwd。

验证补充（本轮完整回归）：

```bash
python3 -m pytest -q
# 940 passed, 2 skipped, 21 warnings in 45.49s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; steps=update_plan,Skill,search_graph,search_graph,get_code_snippet,trace_path,Read,Read; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; steps=Bash,Read,Read,Read,Read,Edit,Bash; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal in tracked/workspace files
# no output
```

### 2026-06-26 Agent Planner store schema：远端多租户状态从字符串检索升级为显式索引

继续按“远端服务，不是本地增强服务”校准。上一轮 `/admin/agent-planner.json` 已支持过滤，但底层仍主要依赖 `session_key` substring；这对多用户/多 workspace 长期运行不够稳，也不利于后续 Admin UI 或 telemetry。

已改造：

- `src/gateway_agent_planner.py`
  - `planner_sessions` SQLite schema 增加显式列：
    - `tenant_key`
    - `workspace_key`
    - `workflow`
    - `current_step`
    - `evidence_count`
  - `_init_db()` 支持兼容迁移：旧表自动 `ALTER TABLE` 补列。
  - 启动/初始化时 bounded backfill 最多 5000 条旧 session，把旧 `state_json/session_key` 补成索引列。
  - 新增索引：
    - `(tenant_key, workspace_key, updated_at DESC)`
    - `(workflow, current_step, updated_at DESC)`
  - `save()` 写入 state_json 的同时同步索引列，并保留 `session_key`。
  - `list_recent()` 不再靠 Python 扫描最近 N 条做过滤，改为 SQL WHERE 查询 `workflow/current_step/session_key/tenant_key/evidence_count`。
- `src/gateway_http_handler.py`
  - `/admin/agent-planner.json` 的过滤能力保持不变，但现在底层使用显式索引列。
  - admin snapshot 额外返回 `tenant_key` / `workspace_key`，便于远端运维定位具体 client scope。
- `tests/test_gateway.py`
  - 新增 `test_agent_planner_store_migrates_and_indexes_runtime_sessions`：验证旧 schema 自动迁移、旧记录 backfill、新记录按显式 tenant/workspace/workflow/current_step/evidence_count 索引查询。
  - 扩展 admin endpoint 测试，验证返回的 `tenant_key`。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_http_handler.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_agent_planner_store_migrates_and_indexes_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_mcp_health_endpoint_supports_probe_query \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result -q
# 4 passed
```

当前意义：Agent Planner runtime 的可观测状态已经更符合远端多租户服务：tenant/workspace/workflow/current_step/evidence 都有持久化索引，不再只靠 session_key 字符串约定。

验证补充（indexed store 本轮完整回归）：

```bash
python3 -m pytest -q
# 941 passed, 2 skipped, 21 warnings in 45.63s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; steps=update_plan,Skill,search_graph,search_graph,get_code_snippet,trace_path,Read,Read; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; steps=Bash,Read,Read,Read,Read,Edit,Bash; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal
# no output
```

### 2026-06-26 Conversation Memory store schema：无限上下文记忆层显式多租户索引

继续按“远端 Agent Planner Runtime”推进。Planner store 已有 tenant/workspace/workflow/current_step 索引；本轮把无限上下文 conversation memory / periodic rollup 也补成显式远端 scope 索引，避免长会话总结跨用户或跨 workspace 串。

已改造：

- `src/gateway_logging.py`
  - `conversation_memories` schema 增加：
    - `tenant_key`
    - `workspace_key`
    - `memory_session_key`
  - `_sqlite_init()` 自动迁移旧 SQLite：旧表补列。
  - 新增索引：
    - `(tenant_key, workspace_key, memory_session_key, ts DESC)`
    - `(kind, tenant_key, workspace_key, memory_session_key, ts DESC)`
- `src/gateway_context.py`
  - 新增 memory index helpers：从 `session_key/workspace_root` 解析 `tenant_key/workspace_key/memory_session_key`。
  - `_sqlite_insert_memory()` 同步写 state 与显式索引列。
  - `_sqlite_recall_memories()` / `_smart_memory_search()` / `_sqlite_latest_rollup()` / `_sqlite_recent_memories_since_rollup()` 改用显式 scope 查询。
  - `_sqlite_tail_memories()` 返回 `tenant_key/workspace_key/memory_session_key`，方便 Admin/API 可观测。
  - 对旧 memory rows 做 bounded backfill，兼容升级前数据。
- `tests/test_gateway.py`
  - 新增 `test_conversation_memory_store_migrates_and_indexes_remote_scope`：覆盖旧 schema 自动迁移、旧记忆 backfill、新记忆写入显式索引、按远端 tenant/workspace/session recall。
  - 原有同 session/workspace recall、huge turn compaction、periodic rollup tests 继续通过。

验证：

```bash
python3 -m py_compile src/gateway_context.py src/gateway_logging.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_store_migrates_and_indexes_remote_scope \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_recalls_same_session_workspace_only \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_compacts_huge_turns_in_sqlite \
  tests/test_gateway.py::NativeGatewayTests::test_agent_planner_store_migrates_and_indexes_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions -q
# 6 passed
```

完整回归：

```bash
python3 -m pytest -q
# 942 passed, 2 skipped, 21 warnings in 46.37s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal
# no output
```

当前意义：无限上下文的记忆与周期总结不再只是按松散 `session_key/workspace_root` 查询，而是有显式 tenant/workspace/session scope；这更符合远端多用户 Agent Runtime 的稳定性要求。

### 2026-06-26 Admin memories filters：无限上下文 memory/rollup 远端可观测面

继续按“远端多租户 Agent Planner Runtime”推进。上一轮 memory store 已有显式 `tenant_key/workspace_key/memory_session_key`；本轮把这些 scope 暴露到 admin API 和 UI，远端服务排障时可以直接查某个用户、workspace、session 的记忆和周期总结。

已改造：

- `src/gateway_context.py`
  - `_sqlite_tail_memories()` 增加过滤：
    - `tenant_contains`
    - `workspace_contains`
    - `session_contains`
    - `kind`
    - `has_rollup=1|0`
  - 查询改为 SQL WHERE，返回仍 bounded，`limit` clamp 到 1..500。
  - 返回项包含 `tenant_key/workspace_key/memory_session_key`。
- `src/gateway_http_handler.py`
  - `/admin/memories.json` 支持 query filters：
    - `?tenant_contains=user-a`
    - `&workspace_contains=workspace-a`
    - `&session_contains=session-a`
    - `&kind=session_rollup`
    - `&has_rollup=1|0`
  - 响应包含 `{ "memories": [...], "filters": {...}, "limit": ... }`。
- `src/gateway_admin.py`
  - Admin UI 的“对话记忆”表格显示 Tenant / Workspace / Session / 类型 / 摘要。
  - UI 文案标注 `/admin/memories.json` 过滤用法。
- `tests/test_gateway.py`
  - 新增 `test_admin_memories_endpoint_filters_remote_scope`：覆盖 admin auth、tenant filter、workspace/session filter、kind/session_rollup filter、has_rollup filter。

验证：

```bash
python3 -m py_compile src/gateway_context.py src/gateway_logging.py src/gateway_http_handler.py src/gateway_admin.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_memories_endpoint_filters_remote_scope \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_store_migrates_and_indexes_remote_scope \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_recalls_same_session_workspace_only \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions -q
# 5 passed
```

完整回归：

```bash
python3 -m pytest -q
# 943 passed, 2 skipped, 21 warnings in 47.09s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal
# no output
```

当前意义：无限上下文不只是内部可用，现在也具备远端服务需要的可观测/排障入口。admin 可以按 tenant/workspace/session/kind 直接查看 memory 和 session_rollup，验证周期总结是否在正确 scope 内工作。

### 2026-06-26 Agent Runtime unified status：Planner + Infinite Context 聚合状态 API

继续按“这不是 gateway，而是远端 Agent Planner Runtime”的目标推进。Planner 和 Memory 已分别具备状态接口，但远端服务排障需要一个统一 runtime surface：同一组 tenant/session/workspace 过滤下，同时看到 planner workflow/evidence 与无限上下文 memory/rollup。

已改造：

- `src/gateway_http_handler.py`
  - 新增 `GET /admin/agent-runtime.json`。
  - 复用 Admin Basic Auth。
  - 支持统一 filters：
    - `tenant_contains`
    - `workspace_contains`
    - `session_contains`
    - `workflow`
    - `current_step`
    - `memory_kind` / `kind`
    - `has_evidence=1|0`
    - `has_rollup=1|0`
    - `limit=1..500`
  - 响应聚合：
    - `runtime.agent_planner.sessions`
    - `runtime.agent_planner.session_count`
    - `runtime.agent_planner.active_workflows`
    - `runtime.memory.memories`
    - `runtime.memory.memory_count`
    - `runtime.memory.rollup_count`
- `src/gateway_admin.py`
  - Admin UI 新增 Agent Runtime 卡片，标注 `/admin/agent-runtime.json` 用法。
- `tests/test_gateway.py`
  - 新增 `test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope`：覆盖鉴权、统一 tenant/session/workflow filters、planner + memory 聚合、bounded 输出、不泄露其他 tenant rollup。

验证：

```bash
python3 -m py_compile src/gateway_http_handler.py src/gateway_admin.py src/gateway_context.py src/gateway_agent_planner.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_memories_endpoint_filters_remote_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled -q
# 4 passed
```

完整回归：

```bash
python3 -m pytest -q
# 944 passed, 2 skipped, 21 warnings in 48.45s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal
# no output
```

当前意义：远端 Agent Runtime 现在有统一状态面。Admin/运维可以用一次请求确认某个 tenant/session/workspace 的 planner 正在什么 workflow/step、积累多少 evidence，以及无限上下文是否产生并注入了正确 rollup。

### 2026-06-26 Agent Runtime event timeline：Planner / Memory 进展事件可查询

继续按“远端 Agent Planner Runtime”目标推进。上一轮有统一 snapshot，但 snapshot 只能看到当前状态；真正排障还需要 timeline：什么时候 planner 切到哪个 step、什么时候产生 memory rollup、这些事件属于哪个 tenant/workspace/session。

已改造：

- `src/gateway_agent_planner.py`
  - `agent_planner.sqlite3` 新增 `runtime_events` 表：
    - `session_key`
    - `tenant_key`
    - `workspace_key`
    - `event_type`
    - `workflow`
    - `step`
    - `summary`
    - `metadata_json`
    - `ts`
  - 新增索引：
    - `(tenant_key, workspace_key, session_key, ts DESC)`
    - `(event_type, workflow, step, ts DESC)`
  - `AgentPlannerStore.save()` 写 planner session 时同步写 `planner_state` event。
  - 新增 `record_runtime_event()` / `list_runtime_events()`，作为 runtime event timeline API。
- `src/gateway_context.py`
  - `_sqlite_insert_memory()` 在写入 `session_rollup` 时同步写 `memory_rollup` runtime event。
- `src/gateway_http_handler.py`
  - `/admin/agent-runtime.json` 响应新增 `runtime.events.items/event_count`。
  - 新增 `GET /admin/agent-runtime-events.json`，支持：
    - `tenant_contains`
    - `workspace_contains`
    - `session_contains`
    - `event_type`
    - `workflow`
    - `step` / `current_step`
    - `limit=1..500`
- `src/gateway_admin.py`
  - Admin UI 的 Agent Runtime 卡片补充事件时间线 API 示例。
- `tests/test_gateway.py`
  - 扩展 `test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope`：覆盖 unified snapshot 中的 event 列表，以及 `/admin/agent-runtime-events.json?event_type=memory_rollup` 过滤查询。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_context.py src/gateway_http_handler.py src/gateway_admin.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_memories_endpoint_filters_remote_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled -q
# 4 passed
```

完整回归：

```bash
python3 -m pytest -q
# 944 passed, 2 skipped, 21 warnings in 49.34s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

secret grep for upstream bearer key literal
# no output
```

当前意义：Agent Runtime 不再只有 snapshot，也有可查询 timeline。远端排障可以按 tenant/workspace/session 追踪 planner state 变化和 memory rollup 生成事件。

### 2026-06-26 远端 Agent Runtime 稳定性补强：Gateway-owned / fallback dispatch 事件

按最新约束重新校正：这不是“本地增强服务”，而是远端多租户 Agent Planner Runtime。Gateway 不能把服务机 cwd 当用户 workspace；用户机器工具默认只下发给 client，由 client 在自己的 workspace 执行。Gateway 只执行明确 Gateway-owned 的服务侧工具（configured HTTP Actions / MCP / built-in service tools）。

本轮补强：

- `src/gateway_tool_runtime.py`
  - 新增 `_agent_runtime_scope_from_request()` / `_record_agent_runtime_request_event()`，复用 Agent Planner 的 tenant/session/workspace session key 规则记录 runtime event。
  - Gateway-owned service tool preexecute 增加两类 timeline event：
    - `gateway_tool_execute`
    - `gateway_tool_result`
  - 非 planner fallback 的 downstream tool request 增加 `tool_dispatch` event，避免无声明工具兼容路径成为不可观测黑盒。
  - event scope 仍按 tenant + session + client workspace 记录，不使用 Gateway 服务机 cwd。
- `tests/test_gateway.py`
  - 新增 `test_gateway_owned_preexecute_records_runtime_events_by_remote_scope`：验证 service-side calculator preexecute 会按远端用户 scope 写 execute/result events。
  - 新增 `test_direct_downstream_fallback_records_remote_runtime_event`：验证无声明工具但需要 client workspace 取证的 fallback 下发路径会写 `direct_downstream_tool_request` dispatch event。

已验证：

```bash
python3 -m py_compile src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_direct_downstream_fallback_records_remote_runtime_event -q
# 2 passed

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_direct_downstream_fallback_records_remote_runtime_event \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_periodically_compacts_evidence_with_llm_summary \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_builtin_calculator_preexecutes_without_request_tools \
  tests/test_gateway.py::NativeGatewayTests::test_parallel_direct_tool_calls_keep_client_workspaces_isolated \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_is_tenant_session_scoped -q
# 10 passed
```

当前意义：远端服务多用户并发时，Planner-managed、Gateway-owned、fallback downstream dispatch 三条路径都进入同一 runtime timeline，可按 tenant/session/workspace 排查。用户 workspace 仍属于 client，不会因为 fallback 或 service-side tool 事件记录而触碰 Gateway cwd。

### 2026-06-26 远端 Agent Runtime 全量验证收口

本轮稳定性补强后重新跑全量验证：

```bash
python3 -m pytest -q
# 947 passed, 2 skipped, 21 warnings in 47.82s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1
# steps=update_plan,Skill,mcp__codebase_memory_mcp__search_graph,mcp__codebase_memory_mcp__search_graph,mcp__codebase_memory_mcp__get_code_snippet,mcp__codebase_memory_mcp__trace_path,Read,Read

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=2
# steps=Bash,Read,Read,Read,Read,Edit,Bash

python3 tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

grep -RIn --exclude-dir=.git --exclude-dir=.gateway_runtime --exclude='.gateway_service.json' --exclude='.case.txt' '<upstream-bearer-key-literal>' . 2>/dev/null || true
# no output
```

结论：当前实现已经按远端 Agent Planner Runtime 方向通过回归：chat-only upstream 只做最终表达；用户 workspace 属于 client；service-side Gateway-owned 工具与 downstream fallback dispatch 都有 tenant/session/workspace scoped runtime timeline；未发现真实 bearer key 泄露到项目文件。

### 2026-06-26 Admin UI 直接展示 Agent Runtime Events

继续按“远端多租户 Agent Planner Runtime”目标推进。上一轮已经把 Planner / Memory / Gateway-owned / fallback dispatch 写入统一 runtime timeline；本轮补齐运维可见性：Admin UI 不再只给 JSON API 链接，而是直接展示最近 runtime events。

修改：

- `src/gateway_admin.py`
  - `_render_admin_ui()` 读取 `list_runtime_events(30)`。
  - `_render_html()` 增加 `runtime_events_data`。
  - Admin 的“兼容性 / Agent Runtime”页新增 `Agent Runtime Events` 表格，展示：
    - 时间
    - event_type
    - workflow
    - step
    - tenant
    - workspace
    - summary
  - 文案明确 timeline 覆盖 planner / memory / Gateway-owned / fallback dispatch。
- `tests/test_gateway.py`
  - 新增 `test_admin_ui_renders_agent_runtime_events_table`，写入一条 `gateway_tool_result` runtime event 后渲染 Admin UI，验证页面包含事件表、workflow、summary、tenant。

验证：

```bash
python3 -m py_compile src/gateway_admin.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_ui_renders_agent_runtime_events_table \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope -q
# 3 passed
```

当前意义：远端服务多用户排障不用先查 SQLite 或单独 curl JSON；Admin 页面可以直接看到 Agent Runtime timeline，确认某个 tenant/workspace/session 的 planner 状态、service-side tool 结果、fallback 下发和 memory rollup 是否正常。

### 2026-06-26 Planner intent 解析隔离 recalled memory，避免无限上下文误触发旧工具

继续按“外层 Agent Planner + chat-only upstream + 无限上下文”目标推进。审计发现一个关键风险：`_inject_recalled_memories()` 会在 Planner 运行前把 `[Gateway recalled memory]` 放进 user content。这样最终 synthesis 能看到历史记忆，但 Planner 的当前意图解析也会看到历史 rollup；如果旧记忆里有“读取 OLD.md / 分析项目 / 修复测试”，当前用户只说 `hi` 时可能误触发下游工具。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `_strip_recalled_memory_blocks()`：只在 Planner intent/路径/validation 判断前剥离 `[Gateway recalled memory]` / `[Conversation Memories]` 块。
  - 新增 `_planner_user_text()`：当前用户意图使用剥离 memory 后的文本。
  - 新增 `_planner_conversation_text()`：Planner conversation 判断使用剥离 memory 后的普通内容，避免旧 rollup 影响 workflow 选择。
  - `plan_downstream_tool_request()` 改用 `_planner_user_text()` / `_planner_conversation_text()`。
  - `_generic_intent_decision()` 再次对传入 `user_text` 做 sanitizer，形成防御式边界。
  - 不修改原始请求 body：recalled memory 仍会进入上游最终 synthesis，不影响无限上下文回答质量。
- `tests/test_gateway.py`
  - 新增 `test_agent_planner_ignores_recalled_memory_for_current_intent`：memory 中有“读取 OLD.md / 分析项目”，当前用户只说 `hi`，Planner 不应下发工具。
  - 新增 `test_agent_planner_prefers_current_request_over_recalled_memory_paths`：memory 中有 `OLD.md`，当前用户要求 `README.md`，Planner 必须读 `README.md`，不能读旧路径。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_recalled_memory_for_current_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_current_request_over_recalled_memory_paths \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction -q
# 3 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_recalls_same_session_workspace_only -q
# 5 passed
```

当前意义：无限上下文继续服务最终回答，但 Agent Planner 的当前工具调度不会被旧 rollup 污染。这个边界对远端多用户服务尤其重要：历史记忆是 evidence，不是当前指令。

### 2026-06-26 Planner anonymous session key 忽略 recalled memory，保证状态归属稳定

继续围绕“远端 Agent Planner + 无限上下文”做边界修正。上一轮已经避免 recalled memory 污染当前 intent；本轮进一步审计发现匿名 planner session key 的 anchor 仍可能被 recalled memory 影响：当消息 content 先插入 `[Gateway recalled memory]` 再跟当前用户请求时，`_planner_anchor_text()` 可能跳过当前请求并 fallback 到带 memory 的 `_last_user_text()`，导致同一个当前请求因为历史 rollup 不同生成不同 anon session key。

风险：

- tenant / workspace 相同，当前请求相同；
- 但 recalled memory 内容不同；
- Planner session key 变化，导致 planner state/evidence/timeline 归属漂移。

本轮修正：

- `src/gateway_agent_planner.py`
  - `_planner_anchor_text()` 对 `/v1/responses` string input 做 `_strip_recalled_memory_blocks()`。
  - `_planner_anchor_text()` 对 message content 做 `_strip_recalled_memory_blocks()` 后再判断是否可作为 anchor。
  - fallback 的 `_last_user_text()` / `_conversation_text()` 也做 memory block stripping。
- `tests/test_gateway.py`
  - 新增 `test_agent_planner_session_key_ignores_recalled_memory_anchor_noise`：同一当前请求，有/无不同 recalled memory，`/v1/messages` planner session key 必须一致。
  - 新增 `test_agent_planner_responses_session_key_ignores_recalled_memory_anchor_noise`：`/v1/responses` string input 同样稳定。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_stays_stable_across_tool_result_turns \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_ignores_recalled_memory_anchor_noise \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_responses_session_key_ignores_recalled_memory_anchor_noise \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_recalled_memory_for_current_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_current_request_over_recalled_memory_paths -q
# 5 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis -q
# 4 passed
```

当前意义：无限上下文继续存在，但不会改变同一当前请求的 Planner anonymous session key；远端多租户服务里的 planner state、evidence、runtime_events 归属更稳定。

### 2026-06-26 Chat-only final synthesis 不再向上游暴露工具 schema / tool_choice

继续按“外层 Agent Planner，chat-only 模型只处理对话内容”目标推进。审计 `_run_tool_orchestration_scoped()` 发现：在 Agent Planner 已经完成下游工具调度、准备最终 synthesis 时，代码仍会无条件调用 `_merge_builtin_tools()`。对 `tools_enabled=adapter` 且上游无 native tool/function 能力的 chat-only upstream，这会把工具 schema 或 text-tool adapter 手册再次注入给上游，等于把工具选择权又交还给弱模型。

本轮修正：

- `src/gateway_tool_runtime.py`
  - 新增 `_chat_only_synthesis_body()`。
  - 当 `_weak_upstream_text_tools_active(mode)` 为真时，最终 upstream request：
    - 移除 `tools`
    - 移除 `tool_choice`
    - 标记 `gateway_context.chat_only_synthesis=true`
    - 标记 `gateway_context.upstream_tools_stripped=true`
  - 只有非 chat-only / native-capable 路径继续 `_merge_builtin_tools()`。
- `tests/test_gateway.py`
  - 扩展 `test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis`：确认最终 upstream body 包含 Planner evidence，但不包含 `tools/tool_choice`，并带有 chat-only synthesis 标记。

验证：

```bash
python3 -m py_compile src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_builtin_calculator_preexecutes_without_request_tools -q
# 3 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_project_analysis_without_path_surfaces_native_tool_fanout_before_upstream \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 4 passed
```

当前意义：chat-only upstream 的职责进一步收窄为最终语言表达；工具 schema、tool_choice、工具手册不会在最终 synthesis 阶段泄漏给弱模型。工具意图解析、能力调度和执行边界由外层 Agent Planner / Gateway Runtime 负责。

### 2026-06-26 Streaming chat-only final synthesis 同步剥离工具 surface

继续收紧“chat-only upstream 只做最终表达”的边界。上一轮修复了非 streaming orchestration，但审计发现 streaming path 的 `_run_streaming_orchestration_scoped()` 仍在最终 synthesis 前无条件 `_merge_builtin_tools()`，会把 native tools 或 text-tool adapter 手册带给 chat-only upstream。

本轮修正：

- `src/gateway_streaming.py`
  - streaming orchestration 引入 `_chat_only_synthesis_body()`。
  - weak/chat-only upstream 路径：`_agent_prepare_upstream_body()` 后直接剥离 `tools/tool_choice` 并标记 `gateway_context.chat_only_synthesis/upstream_tools_stripped`。
  - native-capable / non-chat-only streaming 路径继续 `_merge_builtin_tools()`。
- `tests/test_gateway.py`
  - 扩展 `test_streaming_agent_planner_synthesizes_after_symbol_deep_dive`：确认 streaming final synthesis 上游 request 包含 Planner evidence，但不含 `tools/tool_choice`，并带 chat-only synthesis 标记。

验证：

```bash
python3 -m py_compile src/gateway_streaming.py src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream -q
# 3 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_project_analysis_without_path_surfaces_native_tool_fanout_before_upstream -q
# 3 passed
```

当前意义：streaming 和 non-streaming 两条最终 synthesis 路径现在都符合外层 Agent Planner 设计：上游只接收对话、memory、planner evidence，不再接收工具 schema / tool_choice / 工具手册。

### 2026-06-26 Chat-only final synthesis 阶段禁止弱上游重新获得工具调度权

继续收紧“外层 Agent Planner 负责所有工具/能力调度，chat-only upstream 只做最终表达”的核心边界。上一轮已经在 final synthesis 请求里剥离 `tools/tool_choice`；本轮进一步审计发现：即使请求不给工具，runtime 仍会在收到上游响应后解析 `_extract_tool_calls()` / `_extract_text_tool_calls()` / `_detect_intent_tool_calls()`。如果弱模型在最终回答里输出 JSON 工具请求，旧逻辑仍可能把它转成下游 `tool_use` 或继续工具循环。

本轮修正：

- `src/gateway_tool_runtime.py`
  - 新增 `_chat_only_synthesis_active()`。
  - non-streaming orchestration 在 `gateway_context.chat_only_synthesis=true` 时，收到上游响应后直接作为最终文本响应，不再解析/执行/下发上游伪工具调用。
  - `_attach_request_gateway_context()` 白名单补充 `chat_only_synthesis` / `upstream_tools_stripped`，让最终响应也可观测该边界。
- `src/gateway_streaming.py`
  - streaming orchestration 同步使用 `_chat_only_synthesis_active()`，chat-only final synthesis 响应直接 SSE 输出，不再解析工具调用。
- `src/gateway_agent_planner.py`
  - final synthesis prompt 删除“让上游输出 JSON Edit 工具请求”的旧指令。
  - 新提示明确：不要输出 JSON tool request / function call / tool-use markup；外层 Gateway Agent Planner 拥有全部工具调度权。
- `tests/test_gateway.py`
  - 新增 `test_chat_only_final_synthesis_ignores_upstream_json_tool_request`：弱上游输出 JSON `Edit`，结果只能作为文本返回，不能变成 `tool_use`。
  - 更新旧的 `test_fix_loop_upstream_patch_json_is_*` 为 `test_fix_loop_upstream_patch_json_is_not_granted_tool_authority`：fix_loop 中上游 JSON patch 不再获得工具权限。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_final_synthesis_ignores_upstream_json_tool_request \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_fix_loop_upstream_patch_json_is_not_granted_tool_authority \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 4 passed

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_project_analysis_without_path_surfaces_native_tool_fanout_before_upstream \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_continues_after_codebase_onboarding_skill_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_evidence_survives_upstream_context_compaction \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_multiround_project_analysis_surfaces_next_tools -q
# 4 passed
```

当前意义：最终 synthesis 阶段现在是硬边界：弱上游不能通过 JSON、文本工具标签或 native-like 输出重新发起工具。工具调用必须来自外层 Agent Planner 的 deterministic workflow 或 Gateway-owned service tool preexecute。

### 2026-06-26 Chat-only final synthesis 伪工具尝试可观测但不可执行

用户再次强调这是远端多租户服务，不是本地增强服务；本轮按 Agent Planner Runtime 边界补齐最终 synthesis 可观测性：弱上游如果在 chat-only final synthesis 中继续输出 JSON/native-like/text tool call，Gateway 只记录 `upstream_tool_attempt_ignored` runtime event，不会解析为下游 `tool_use`，也不会执行。

本轮修正：

- `src/gateway_tool_runtime.py`
  - 新增 `_record_ignored_upstream_tool_attempt()`。
  - chat-only synthesis 响应中检测到 native/text tool attempt 时，记录 workflow=`chat_only_synthesis`、step=`ignore_upstream_tool_attempt`。
  - event metadata 标记 `tool_authority_granted=false`，并限制 payload/response preview，避免远端服务事件表被弱上游大文本撑爆。
  - non-streaming final synthesis 保持硬边界：记录后直接返回最终文本。
- `src/gateway_streaming.py`
  - streaming final synthesis 同步记录 ignored upstream tool attempt。
  - SSE 输出仍是最终文本，不产生 `tool_use` / `tool_start`。
- `tests/test_gateway.py`
  - non-streaming JSON `Edit` 伪工具请求：只作为文本返回，同时记录 ignored event。
  - streaming symbol deep-dive final synthesis：弱上游输出 JSON `Edit`，只作为 SSE 文本返回，同时记录 ignored event。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_final_synthesis_ignores_upstream_json_tool_request \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 2 passed
```

当前意义：chat-only upstream 仍然只是语言合成器。即便它“想调用工具”，外层远端 Agent Planner 也只把这件事作为可观测安全事件记录；工具权限不会被授予弱上游，client workspace 也不会被服务端误操作。

### 2026-06-26 全量回归复核：chat-only 边界只应用于 Planner-owned final turn

全量回归发现一个重要边界：`tools_enabled=adapter/auto` 不等于所有请求都已经进入 Agent Planner final synthesis。旧的 native/text 工具编排测试仍需要允许 upstream tool call loop；真正要硬切断工具权限的是已经有 `gateway_context.strategy=agent_planner_final_synthesis` 或 planner-owned evidence 的最终综合回合。

本轮修正：

- `src/gateway_tool_runtime.py`
  - 新增 `_should_use_chat_only_synthesis_boundary()`。
  - 只有 Agent Planner / Gateway-owned service tool 已产生 evidence 的 final turn 才加 `chat_only_synthesis` 硬边界。
  - 旧的 native/tool-loop orchestration 继续可解析和执行合法工具调用。
- `src/gateway_streaming.py`
  - streaming 路径使用同一判定，避免把普通 streaming orchestration 错误变成 final synthesis。
- `tests/test_gateway.py`
  - 对 native-capable alias calculator 回归显式声明 native upstream 能力，避免被默认 chat-only 配置的 Gateway-owned preexecute 抢先处理。
- `tests/integration/agent_planner_multiround_smoke.py`
  - 更新为安全闭环：弱上游 JSON `Edit` 只作为文本返回并记录 ignored event，不再期待 chat-only upstream 获得 Edit 权限。

验证：

```bash
python3 -m pytest -q
# 953 passed, 2 skipped, 21 warnings in 47.89s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=1; ignored_upstream_tool_attempt=Edit

git diff --check
# pass

grep -RIn --exclude-dir=.git --exclude-dir=.gateway_runtime --exclude='.gateway_service.json' --exclude='.case.txt' 'sk-REDACTED' .
# no output
```

当前意义：远端服务不会把普通请求误判成 final synthesis，也不会让 chat-only final synthesis 重新获得工具权限；多用户 runtime 事件仍按 client tenant/session/workspace scope 记录。

### 2026-06-26 Project-scope CLI smoke 复核

继续复核“这是远端服务，不是本地增强服务”的关键边界：Gateway 服务机目录不能泄漏为用户 workspace；Claude/Codex downstream 必须在 client project scope 执行。

验证：

```bash
python3 tests/integration/project_scope_cli_smoke.py
# pass=true; claude.ok=true; codex.ok=true
# memory_service_root_leak=false; direct_list_leaks_service_skills=false
```

### 2026-06-26 Agent Runtime planner workspace filter 补齐

继续按“远端多租户服务，workspace 属于 client”复核 Admin/Runtime 可观测面时发现：`/admin/agent-runtime.json` 和 `/admin/agent-planner.json` 已支持 tenant/session 过滤，但 planner session 列表没有真正按 `workspace_contains` 过滤；这会导致同一租户多个 client workspace 并发时，管理面看到混合 planner state。

本轮修正：

- `src/gateway_agent_planner.py`
  - `AgentPlannerStore.list_recent()` 新增 `workspace_contains` SQL 过滤，直接命中显式索引列 `workspace_key`。
- `src/gateway_http_handler.py`
  - `/admin/agent-planner.json` 接收并回显 `workspace_contains`。
  - `/admin/agent-runtime.json` 的 planner session 查询同步传入 `workspace_contains`，与 memory/events 过滤一致。
- `tests/test_gateway.py`
  - 覆盖 planner endpoint workspace 过滤。
  - 覆盖 agent-runtime 聚合接口在同 tenant 不同 workspace 下不会混入其他 workspace planner evidence。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_http_handler.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_planner_endpoint_lists_runtime_sessions \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope -q
# 2 passed
```

当前意义：远端多用户/多 workspace 同时请求时，Agent Runtime 管理面可以按 client workspace 精确隔离 planner state、memory、events，不会把 Gateway 服务机或其他 client workspace 混入当前排查视图。

### 2026-06-26 Agent Planner capability registry 可观测面

继续把系统从兼容 gateway 推向远端 Agent Runtime：外层 planner 不应只是隐式 if/else 调度工具，运维和客户端需要能查询“这个 Agent 当前有哪些能力、哪些能力由 Gateway 服务端执行、哪些必须下发到 client workspace”。

本轮修正：

- `src/gateway_tool_runtime.py`
  - 新增 `planner_capability_catalog()`。
  - 输出 workflows、service-side capabilities、downstream-owned capabilities、HTTP Actions、MCP servers、ownership model 和 counts。
  - 明确三方边界：Gateway service / downstream client / chat-only upstream。
- `src/gateway_http_handler.py`
  - 新增 `/admin/agent-capabilities.json`。
  - `/admin/agent-runtime.json` 内嵌 `runtime.capabilities`，让 runtime status 一次返回 state/memory/events/capabilities。
- `tests/test_gateway.py`
  - 覆盖 capabilities endpoint：`calculator`、HTTP Action `get_weather` 属于 Gateway service；`Read`、`Bash` 属于 downstream client。
  - 覆盖 agent-runtime 聚合响应包含 capability ownership model。

验证：

```bash
python3 -m py_compile src/gateway_tool_runtime.py src/gateway_http_handler.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope -q
# 2 passed
```

当前意义：Agent Planner 的功能调度边界现在有正式可查询的能力注册表，不再只能从代码或日志推断。chat-only upstream 仍是 synthesis-only，用户机器工具继续绑定 client workspace，Gateway-owned service tools 作为 planner service capability 暴露。

### 2026-06-26 Planner decision history 持久化

继续补 Agent Runtime 的“决策可审计性”：之前 runtime timeline 能看到 `tool_dispatch` event，但 planner state snapshot 本身只展示当前 step / evidence；如果只看 session snapshot，无法知道这个 Agent 为什么走到当前步骤、最近下发了哪些工具。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 bounded `decision_history` / `last_decision`。
  - 每次 `_planner_decision()` 记录 workflow、step、reason、call_count、tool call id/name、arguments preview、timestamp。
  - 决策历史最多保留 20 条，snapshot 只暴露最近 10 条且不暴露完整大参数。
  - 决策历史写入 `.gateway_runtime/agent_planner.sqlite3`，Admin runtime session snapshot 可查询。
- `tests/test_gateway.py`
  - 在 runtime event 测试中验证 `planner_state_snapshot()` 和 persisted `list_recent()` 都能看到 `last_decision`。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_project_analysis_without_path_surfaces_native_tool_fanout_before_upstream -q
# 2 passed
```

当前意义：Planner 不再只是“返回下一步工具调用”；它的每次调度决策会进入可查询 state snapshot。远端多用户排障时，可以同时看当前状态、runtime timeline、最近决策历史。

### 2026-06-26 Agent Planner workflow registry 状态图

继续减少“gateway 兼容层”味道：之前 workflow steps 同时散落在 `_planner_plan_items()` 和 capability catalog，新增 workflow 时容易只改一处。真正的 Agent Planner 应该有统一 workflow registry，既驱动 Todo/update_plan，也暴露给 runtime capability surface。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `WORKFLOW_REGISTRY`。
  - 新增 `planner_workflow_catalog()`。
  - `_planner_plan_items()` 改为从 registry 读取 plan items。
  - registry 统一描述 workflow owner、description、steps、plan_items。
- `src/gateway_tool_runtime.py`
  - `planner_capability_catalog()` 的 workflows 改为来自 `planner_workflow_catalog()`，不再维护第二份 hardcoded workflow list。
- `tests/test_gateway.py`
  - capabilities endpoint 测试确认 `project_analysis` workflow 的 owner、steps、plan_items 都来自正式 registry。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_emits_update_plan_before_project_tools_when_declared -q
# 2 passed
```

当前意义：Agent Planner 的 workflow 图开始从散落 if/else 提升为可查询、可扩展的 registry；后续扩展新 workflow 时可以先注册状态图，再接入 intent 和 tool dispatch。

### 2026-06-26 Planner intent classification 可观测性

继续按远端 Agent Planner，而不是本地增强服务，补齐“当前意图解析”这层 runtime surface。之前 planner 会通过散落条件分支下发工具，但 Admin/runtime snapshot 只能看到 workflow/current_step/decision_history，看不到本轮到底识别成什么意图、置信度、信号来源，也不利于多用户远端排障。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `PlannerIntent` 和 `classify_planner_intent()`。
  - 意图分类会先剥离 `[Gateway recalled memory]`，避免无限上下文 recall 抢占当前用户指令。
  - 当前覆盖 `project_analysis`、`validation`、`code_search`、`edit/write`、`skill_request`、`shell_command`、`read_file`、`list_directory`、`web_search`、`custom_function`、`plain_chat`。
  - `plan_downstream_tool_request()` 在任何工具调度前持久化 intent。
  - `planner_state_snapshot()` 暴露 bounded `intent` / `intent_history`。
  - runtime timeline 新增 `intent_classification` 事件。
- `tests/test_gateway.py`
  - 验证 project analysis 会在 state snapshot、persisted session、runtime events 里记录 intent。
  - 验证 recalled memory 中的旧 `OLD.md` 不会把当前 `hi` 误判成 read/project intent。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_recalled_memory_for_current_intent -q
# 2 passed
```

当前意义：远端多租户服务可以通过 Admin/runtime API 看到“当前用户请求被识别为什么意图、为什么进入某 workflow、是否因为缺少下游工具而没有调度”。这一步不改变 chat-only upstream 的权限边界；上游仍只做 synthesis，工具执行权仍属于 Gateway-owned service tools 或 downstream client workspace。

### 2026-06-26 Intent registry + 并发 Planner Store 修复

继续按“远端 Agent Planner Runtime”推进，而不是本地增强 shim。本轮把上一轮新增的结构化 intent 从单纯可观测字段进一步提升为正式能力注册和调度输入。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `INTENT_REGISTRY` 和 `planner_intent_catalog()`，把 `project_analysis`、`validation`、`code_search`、`edit/write`、`skill_request`、`shell_command`、`read_file`、`list_directory`、`web_search`、`custom_function`、`plain_chat` 注册为一等 Planner intent。
  - `_generic_intent_decision()` 现在优先消费 `classify_planner_intent()` 的 `kind/workflow`，不再完全依赖重复散落的 if/else 重新判断。
  - 修复 `_STORE` 懒加载竞态：新增 `_STORE_LOCK`，避免远端服务多用户首请求并发时创建多个 `AgentPlannerStore` 实例并抢同一个 SQLite 导致 `database is locked`。
- `src/gateway_tool_runtime.py`
  - `planner_capability_catalog()` 现在暴露 `intents` 和 `counts.intents`，Admin/Runtime surface 能同时看到 workflow registry 与 intent registry。
- `tests/test_gateway.py`
  - capabilities endpoint 验证 intent registry 暴露 `project_analysis/plain_chat`。
  - 新增并发远端隔离测试：两个用户、两个 client workspace 同时请求 `Read README.md`，要求下发工具参数、persisted intent、tenant/workspace index 均互不串线。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_runtime_events_record_dispatch_result_and_compaction \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_recalled_memory_for_current_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_current_request_over_recalled_memory_paths \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# passed
```

重要发现：新增并发测试第一次运行复现了真实远端稳定性问题：`database is locked`。根因不是 SQLite WAL 本身，而是 `_STORE` 单例初始化无锁导致两个 store 实例各自持锁。已通过 `_STORE_LOCK` 修复并验证。

### 2026-06-26 Project-analysis transition registry 状态机化

继续把网关从 tool-call shim 推向远端 Agent Planner。本轮处理 project analysis workflow 仍在 `plan_downstream_tool_request()` 内硬编码 Step 1/2/3/4/5 的问题，把项目分析流程改为显式 transition registry。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `PROJECT_ANALYSIS_TRANSITIONS`。
  - `WORKFLOW_REGISTRY["project_analysis"]` 现在包含 `transitions`。
  - `planner_workflow_catalog()` 会对外暴露 transition step / condition / builder / reason。
  - 新增 `_project_analysis_transition_decision()`，按 transition registry 统一评估状态转移。
  - `plan_downstream_tool_request()` 不再直接堆 Step 1-5 分支，而是调用 transition evaluator。
- `tests/test_gateway.py`
  - capability endpoint 现在验证 `project_analysis.transitions` 被公开。
  - 保持已有 core_flow_trace / symbol_deep_dive / project_structure 行为不回退。

当前 transition 顺序：

1. `planner_progress`：先发布计划。
2. `codebase_onboarding`：有 Skill 且可用 `codebase-onboarding` 时先加载技能。
3. `project_structure`：Skill 后收集结构；无 Skill 客户端也有 fallback。
4. `core_flow_trace`：结构后追踪核心请求流。
5. `symbol_deep_dive`：根据 graph evidence 读取/追踪关键符号。
6. `key_file_read`：读取 README/配置/关键源码。
7. `synthesis`：无更多 transition 时进入 chat-only final synthesis。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_codebase_search_graph_for_project_structure -q
# 4 passed
```

意义：project_analysis workflow 现在从“函数里的硬编码步骤”向“可查询状态图 + transition evaluator”演进。后续新增文档生成、安全审计、部署验证等 workflow 时，可以照这个 registry/transition 结构扩展，而不是继续在主调度函数里堆条件。

### 2026-06-26 通用 workflow transition engine

继续推进“真正 Agent Planner”抽象：上一轮已经把 `project_analysis` 变成 transition table，但 evaluator 仍是 project 专用函数。本轮将状态转移执行器抽成通用 engine，后续 `fix_loop`、`test_build`、`code_search` 可以复用同一套机制。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `_workflow_transition_decision()` 通用状态机执行器。
  - 新增 `TransitionCondition` / `TransitionBuilder` handler 类型。
  - `project_analysis` 现在通过 `PROJECT_ANALYSIS_CONDITIONS` 与 `PROJECT_ANALYSIS_BUILDERS` 接入通用 engine。
  - 删除旧的 project 专用 `_project_analysis_build_calls()`，避免保留两套路由实现。
  - `_project_analysis_transition_decision()` 现在只负责构建上下文并调用通用 transition engine。

当前意义：Agent Planner 的核心路径进一步变成：

```text
intent registry -> workflow registry -> transition table -> condition/builder handlers -> downstream dispatch
```

而不是每个 workflow 在主调度函数里堆一组 if/else。下一步可以把 `fix_loop` 的 `diagnostic_read/source_followup_read/qa_loop` 迁入同一 engine。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_traces_core_flow_after_planner_structure_step \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_deep_dives_symbol_after_core_flow_trace \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_prefers_codebase_search_graph_for_project_structure \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# 5 passed
```

### 2026-06-26 fix_loop / qa_loop 迁入通用 transition engine

继续把 Agent Planner 从 project-analysis 单点能力扩展到完整修复闭环。本轮将失败诊断和编辑后验证从 `_generic_intent_decision()` 的散落 if/else 中迁入通用 workflow transition engine。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `FIX_LOOP_TRANSITIONS`：
    - `diagnostic_read`：失败 evidence 中出现文件路径时读取失败文件。
    - `source_followup_read`：diagnostic read 后根据源码 import 继续读取关联源码。
  - 新增 `QA_LOOP_TRANSITIONS`：
    - `validate_after_test`：Edit/Write 后重新跑测试。
    - `validate_after_build`：Edit/Write 后重新跑构建/类型检查。
  - `WORKFLOW_REGISTRY.fix_loop.transitions` / `qa_loop.transitions` 对外暴露。
  - 新增 `FIX_LOOP_CONDITIONS` / `FIX_LOOP_BUILDERS` / `QA_LOOP_CONDITIONS` / `QA_LOOP_BUILDERS`。
  - `_generic_intent_decision()` 删除旧的 QA/fix 三段 if 分支，改为调用 `_fix_qa_transition_decision()`。
- `tests/test_gateway.py`
  - capability endpoint 验证 `fix_loop` 与 `qa_loop` transitions 被公开。
  - 新增 `test_fix_loop_reads_source_followup_import_after_diagnostic_read`，确认失败读文件后会继续读取 import 关联源码。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py tests/test_gateway.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_reads_failure_file_after_test_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_fix_loop_reads_source_followup_import_after_diagnostic_read \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_qa_loop_reruns_tests_after_edit_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_qa_loop_passes_to_final_synthesis_after_validation_success \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_fix_loop_upstream_patch_json_is_not_granted_tool_authority -q
# 6 passed
```

意义：修复闭环开始真正变成 Agent Planner workflow：失败 -> 诊断读文件 -> 读关联源码 -> chat-only synthesis / fail-closed patch boundary -> Edit 后验证，而不是散落在 gateway adapter 判断里。

### 2026-06-26 code_search / test_build 初始调度迁入通用 transition engine

继续按“远端 Agent Planner/Runtime”边界收口：`code_search` 与 `test_build` 不能继续留在 `_generic_intent_decision()` 的手写 if/else 里，否则 Gateway 仍像 tool-call adapter，而不是可查询、可扩展的 planner 状态机。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `CODE_SEARCH_TRANSITIONS`，把 `code_search` 初始搜索调度纳入 `_workflow_transition_decision()`。
  - 新增 `TEST_BUILD_TRANSITIONS`，把 `run_test` / `run_build` 初始验证调度纳入 `_workflow_transition_decision()`。
  - `WORKFLOW_REGISTRY.code_search.transitions` / `test_build.transitions` 对外暴露。
  - 新增 `CODE_SEARCH_CONDITIONS` / `CODE_SEARCH_BUILDERS` / `TEST_BUILD_CONDITIONS` / `TEST_BUILD_BUILDERS`。
  - `_generic_intent_decision()` 删除旧的 code_search/test_build 手写 dispatch 分支，改为调用 `_code_search_transition_decision()` 与 `_test_build_transition_decision()`。
- `tests/test_gateway.py`
  - capability endpoint 增加断言，确认 `code_search` 与 `test_build` 的 transition table 已公开。

边界保持：

- chat-only upstream 仍只做最终 synthesis，不获得工具权限。
- `Read/Edit/Bash/Skill/search_graph` 等用户机器工具仍只作为 downstream tool request 返回，不在 Gateway 服务机默认执行。
- transition state 仍通过 tenant/workspace/session scoped planner store 保存，避免多用户串状态。

当前验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_code_search_infers_mcp_project_argument \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_run_tests_uses_declared_shell_tool -q
# 3 passed
```

补充全量验证：

```bash
python3 -m pytest -q
# 956 passed, 2 skipped, 21 warnings in 48.54s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1; final_text="项目分析完成：PROJECT-ANALYSIS-FINAL-OK"

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=1; ignored_upstream_tool_attempt=Edit

python3 tests/integration/project_scope_cli_smoke.py
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

grep -RIn --exclude-dir=.git --exclude-dir=.gateway_runtime --exclude='.gateway_service.json' --exclude='.case.txt' '<redacted-upstream-key>' . 2>/dev/null || true
# no output
```

### 当前 Agent Planner 收口状态

- `project_analysis`、`code_search`、`test_build`、`fix_loop`、`qa_loop` 均已经有公开 transition table，并通过同一个 `_workflow_transition_decision()` 执行。
- Gateway 仍按远端服务模型工作：planner state / runtime events / memory 均按 tenant、client workspace、session scoped；用户机器工具只下发给 downstream client 执行。
- chat-only upstream 继续保持 synthesis-only：最终轮 strip `tools/tool_choice`，忽略并记录上游伪工具尝试。

### 2026-06-26 generic_tool / edit 也迁入通用 transition engine

继续按“远端 Agent Planner/Runtime”目标推进：上一轮已经把 `project_analysis`、`code_search`、`test_build`、`fix_loop`、`qa_loop` 迁入 transition engine。本轮把剩余常用 fallback 入口也从 `_generic_intent_decision()` 的手写 if/else 中移出。

本轮修正：

- `src/gateway_agent_planner.py`
  - 新增 `GENERIC_TOOL_TRANSITIONS`：`skill_request`、`shell_command`、`read_file`、`list_directory`、`web_search`、`custom_function`。
  - 新增 `EDIT_TRANSITIONS`：`edit_file`、`write_file`。
  - `WORKFLOW_REGISTRY.generic_tool.transitions` / `edit.transitions` 对外暴露。
  - 新增 `GENERIC_TOOL_CONDITIONS` / `GENERIC_TOOL_BUILDERS` / `EDIT_CONDITIONS` / `EDIT_BUILDERS`。
  - `_generic_intent_decision()` 删除 skill/shell/read/list/web/custom/edit/write 旧手写 dispatch 分支，改为调用 `_generic_tool_transition_decision()` 与 `_edit_transition_decision()`。
  - generic/edit transitions 默认在已有 evidence 后停止重复下发工具，让 chat-only upstream 进入 evidence synthesis。
- `tests/test_gateway.py`
  - capability endpoint 增加断言，确认 `generic_tool` 与 `edit` 的 transition table 已公开。

边界保持：

- 这些 transition 只产生 downstream client tool request，不在 Gateway 服务机默认执行用户文件/shell/GUI 工具。
- Gateway-owned HTTP Action / MCP / built-in service tool 仍留在 gateway-owned executor 路径，`custom_function` 不抢占这些服务端工具。
- chat-only upstream 仍无工具权限；有 evidence 后进入 synthesis，不重复执行相同 downstream 工具。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_skill_uses_declared_claude_code_skill_schema \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_repeat_read_after_tool_result \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_repeat_responses_read_after_function_output \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_web_search_uses_declared_downstream_tool_name \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_chat_only_custom_function_call_is_surfaced_without_upstream_native_support \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_explicit_edit_uses_declared_edit_tool \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_run_tests_uses_declared_shell_tool \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_code_search_infers_mcp_project_argument -q
# 9 passed
```

补充全量验证：

```bash
python3 -m pytest -q
# 956 passed, 2 skipped, 21 warnings in 48.92s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; steps include update_plan, Skill, search_graph, get_code_snippet, trace_path, Read; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; ignored_upstream_tool_attempt=Edit

python3 tests/integration/project_scope_cli_smoke.py
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false; direct_list_leaks_service_skills=false

git diff --check
# pass

grep ... '<redacted-upstream-key>'
# no output
```

### 2026-06-26 Responses 无限上下文注入修复 + 远端压力 smoke

继续按“远端 Agent Planner/Runtime”目标补强，不只做工具协议 shim。本轮重点验证多用户远端服务和上游上下文有限时的记忆召回链路。

本轮修正：

- `src/gateway_context.py`
  - 修复 `/v1/responses` 的 recalled memory 注入位置。
  - 之前 memory 注入到 `messages`；但 Responses 请求权威字段是 `input`，当上游协议为 OpenAI Chat 时 `_responses_to_chat_payload()` 会从 `input` 转换，导致 recalled memory 可能不会进入上游。
  - 现在 `/responses` 会把 `[Gateway recalled memory]` 注入 `input`：string input 会转成 system+user input list，list input 会 prepend/merge system memory。
- `tests/test_gateway.py`
  - 新增 `test_responses_conversation_memory_is_injected_into_input`，证明 Responses API 路径会把 recalled memory 带到最终上游请求。
- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - 新增远端 Agent Runtime 压力 smoke：6 个 tenant / 6 个 client workspace 并发触发 Planner `Read` downstream tool request；随后每个用户各自写入对话记忆、触发 periodic rollup，并在 recall turn 验证只召回自己的 marker。
  - 同时检查 planner runtime event、planner session、memory rollup 都按 tenant/workspace/session 过滤，不串用户、不串 workspace。

验证：

```bash
python3 -m py_compile src/gateway_context.py src/gateway_agent_planner.py tests/test_gateway.py tests/integration/agent_planner_remote_pressure_smoke.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_responses_conversation_memory_is_injected_into_input \
  tests/test_gateway.py::NativeGatewayTests::test_conversation_memory_periodic_rollup_is_recalled \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# 3 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6
```

补充全量验证：

```bash
python3 -m pytest -q
# 957 passed, 2 skipped, 21 warnings in 48.79s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; ignored_upstream_tool_attempt=Edit

python3 tests/integration/project_scope_cli_smoke.py
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6

git diff --check
# pass

# bearer token literal audit
# no output outside ignored local runtime/config files
```

### 2026-06-26 Streaming Responses memory recall + admin endpoint pressure verification

继续补齐远端 Agent Runtime 的完整链路：非流式 Responses memory 已修复后，本轮进一步覆盖 streaming Responses 和后台可观测 API。

本轮修正/增强：

- `tests/test_gateway.py`
  - 新增 `test_streaming_responses_conversation_memory_is_injected_before_upstream`。
  - 先用 `/v1/responses` 写入对话记忆，再走 streaming `/v1/responses` recall，验证 recalled memory 在 upstream OpenAI Chat payload 中出现，并最终以 SSE 输出。
  - 这个测试首跑暴露了 scoped streaming 测试必须稳定 client workspace；已改为显式 client workspace，符合远端服务模型。
- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - 扩展压力 smoke：在 6 tenant / 6 workspace 并发 planner + memory rollup + recall 之后，启动真实 `GatewayHandler` HTTP admin server。
  - 用 Basic Auth 查询：
    - `/admin/agent-runtime.json`
    - `/admin/memories.json`
    - `/admin/agent-runtime-events.json`
  - 验证 admin API 按 tenant/workspace/session/event_type 过滤，不泄漏其他用户 marker。

验证：

```bash
python3 -m py_compile src/gateway_context.py tests/test_gateway.py tests/integration/agent_planner_remote_pressure_smoke.py
# pass

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_streaming_responses_conversation_memory_is_injected_before_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_responses_conversation_memory_is_injected_into_input \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_memories_endpoint_filters_remote_scope -q
# 4 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true
```

补充全量验证：

```bash
python3 -m pytest -q
# 958 passed, 2 skipped, 21 warnings in 48.92s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; ignored_upstream_tool_attempt=Edit

python3 tests/integration/project_scope_cli_smoke.py
# pass=true; claude.ok=true; codex.ok=true; responses_stream_skill_ok=true; memory_service_root_leak=false

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true

git diff --check
# pass

# bearer token literal audit
# no output outside ignored local runtime/config files
```

### 2026-06-26 Remote Agent Planner runtime-scope hardening

按“远端 Agent Planner / Agent Runtime”重新审计 streaming 和非 streaming 的 runtime scope 一致性：发现 streaming 入口虽然已解析 client workspace，但进入 `_workspace_scope` 时没有携带完整 request body，因此内置工具 runtime scope 可能退化成仅 workspace 维度。已修正为与非流式路径一致，runtime scope 包含 tenant/session/workspace，避免多用户并发时 caller-visible session id 冲突。

修改：

- `src/gateway_streaming.py`
  - `run_streaming_orchestration()` 进入 `_workspace_scope(_request_workspace_root(body), body)`。
- `tests/test_gateway.py`
  - 新增 `test_streaming_entry_sets_remote_runtime_scope_from_request_body`，直接断言 streaming 入口设置的 scope 包含 `tenant:stream-user`、`session:stream-session` 和 workspace hash。
- `docs/agent-runtime-architecture.md`
  - 固化远端 Agent Planner / Runtime 架构、边界、验收证据和剩余风险。

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_entry_sets_remote_runtime_scope_from_request_body \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_gateway_owned_builtin_calculator_preexecutes_before_upstream \
  tests/test_gateway.py::NativeGatewayTests::test_gateway_owned_preexecute_records_runtime_events_by_remote_scope \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated -q
# 4 passed

python3 -m pytest -q
# 959 passed, 2 skipped, 21 warnings in 48.97s

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true

git diff --check
# pass

# bearer token literal audit
# no output outside ignored local runtime/config files
```

### 2026-06-26 Long-context remote Agent Runtime pressure hardening

继续按“远端 Agent Planner / Runtime”目标推进，重点验证上游 chat-only 且上下文窗口很小时，Gateway/Planner 是否真的能靠自身 memory rollup + recall + compaction 支撑多用户长上下文。

本轮 smoke 首跑暴露两个真实缺口：

1. `/v1/responses` 的 `input` 是 list 时，旧消息会被 summary，但 recent item 中 `{role, content: "超长字符串"}` 没有被裁剪，导致请求虽然标记 `compacted=true`，实际仍可能把弱上游上下文撑爆。
2. Streaming direct tool dispatch 前，Planner 会先对 memory-injected 的巨型当前输入做意图解析；如果没有有界化，大输入会拖慢 `_extract_paths()` 等正则分类路径。

修正：

- `src/gateway_context.py`
  - `_trim_content_for_context()` 现在递归裁剪 dict/list/string content，覆盖 Responses/OpenAI 常见 `{role, content: "..."}` 结构。
  - `_compact_messages_with_summary()` 在保留 recent messages 时也会裁剪 recent content，不再只压缩旧消息。
- `src/gateway_agent_planner.py`
  - 新增 `_bounded_planner_text()`，intent classification 只处理有界 head/tail 文本，避免远端长上下文请求在工具规划前被巨大输入拖慢。
- `tests/test_gateway.py`
  - 新增 Responses recent item 裁剪回归。
  - 新增巨大 plain chat planner 分类回归。
- `tests/integration/agent_planner_long_context_pressure_smoke.py`
  - 新增长上下文压力 smoke：4 tenant / 4 client workspace 并发写入大上下文记忆；周期 rollup；streaming `/v1/responses` 在小上下文窗口下 recall；验证 upstream payload 已 compact、只含自己 marker、不泄漏其他 tenant marker。

验证：

```bash
python3 -m pytest tests/test_gateway.py::ContextSummarizationTests::test_responses_input_list_compaction_trims_large_recent_item_content \
  tests/test_gateway.py::ContextSummarizationTests::test_responses_input_list_compaction_keeps_summary_and_recent_items \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_bounds_huge_plain_chat_before_intent_regexes -q
# 3 passed

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; rollups_checked=4; streaming_responses_recall_checked=4; compaction_checked=true; cross_tenant_leak_checked=true
```

### 2026-06-26 Chat-only synthesis boundary runtime event

继续推进“chat-only 上游只处理对话内容，Agent Planner 拥有工具权限”的远端可观测性。本轮补充运行时事件，明确记录 Planner 何时把最终综合请求切到 chat-only boundary 并剥离工具面。

修改：

- `src/gateway_tool_runtime.py`
  - 新增 `_record_chat_only_synthesis_boundary_event()`。
  - 非流式最终综合调用 `_chat_only_synthesis_body()` 后记录 `chat_only_synthesis_boundary` 事件。
- `src/gateway_streaming.py`
  - streaming 最终综合同样记录 `chat_only_synthesis_boundary` 事件。
- `tests/test_gateway.py`
  - 非流式 final synthesis 测试验证事件：`workflow=chat_only_synthesis`、`step=strip_upstream_tools`、`tool_authority_granted=false`。
  - streaming symbol deep-dive final synthesis 测试同时验证 boundary event 和 ignored upstream tool attempt event。

验证：

```bash
python3 -m pytest tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis -q
# 2 passed
```

### 2026-06-26 Agent Runtime requirement audit surface

按用户最新要求重新以“远端 Agent Planner / Runtime”审视：这不是本地增强服务，client workspace 必须属于远端调用方；多用户并发时必须能从机器可读证据判断当前 scope 是否稳定、是否泄漏、是否仍把 chat-only upstream 当工具模型使用。

本轮补齐：

- `src/gateway_http_handler.py`
  - 新增 `/admin/agent-runtime-audit.json`。
  - 审计输入只使用已经按 `tenant_contains` / `workspace_contains` / `session_contains` 过滤后的 planner sessions、runtime events、memories，不扩大查询范围。
  - 输出 8 个需求项：chat-only synthesis-only、planner owns intents/workflows、downstream client workspace tools、gateway-owned service tools、infinite context memory rollup、tenant/workspace isolation、streaming/non-streaming parity、admin observability。
- `src/gateway_admin.py`
  - Agent Runtime 卡片增加 requirement audit API 入口。
- `tests/test_gateway.py`
  - 新增 scoped audit 回归：种入当前 tenant/workspace/session 的 planner/event/memory 证据，同时种入其他 tenant marker，验证审计结果全为 `proven/current_scope` 且不泄漏其他 tenant 或服务 workspace 路径。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements -q
# 1 passed
```

最终验证补充：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_endpoint_combines_planner_and_memory_scope \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_capabilities_endpoint_exposes_ownership_model \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_injects_compact_evidence_before_final_upstream_synthesis \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_agent_planner_synthesizes_after_symbol_deep_dive -q
# 5 passed

python3 -m pytest -q
# 962 passed, 2 skipped, 21 warnings in 49.86s

python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/project_scope_cli_smoke.py
python3 tests/integration/agent_planner_remote_pressure_smoke.py
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# all ok=true/pass=true; remote pressure users=6; long-context users=4; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

### 2026-06-26 Remote pressure smoke now covers Agent Runtime audit

继续按“远端 Agent Planner / Runtime，不是本地增强服务”推进。本轮把 `/admin/agent-runtime-audit.json` 纳入真实远端并发 smoke，而不是只依赖单元测试。

修改：

- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - 在 6 tenant / 6 client workspace 并发读文件、memory rollup、recall 的基础上，额外让 admin scope 执行 Gateway-owned `calculator` 请求。
  - 再通过 HTTP 查询 `/admin/agent-runtime-audit.json`，验证当前 tenant/workspace/session scope 下：
    - `chat_only_upstream_synthesis_only` 为 `proven/current_scope`；
    - `planner_owns_intent_and_workflows` 为 `proven/current_scope`；
    - `downstream_client_workspace_tools` 为 `proven/current_scope`；
    - `gateway_owned_service_tools` 为 `proven/current_scope`；
    - `infinite_context_memory_rollup` 为 `proven/current_scope`；
    - `tenant_workspace_isolation` 为 `proven/current_scope`；
    - `admin_observability` 为 `proven/current_scope`；
    - audit payload 不泄漏其他 tenant memory marker。

验证：

```bash
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true; admin_audit_checked=true
```

复验补充：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements -q
# 1 passed

python3 -m pytest -q
# 962 passed, 2 skipped, 21 warnings in 49.96s

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

### 2026-06-26 Remote pressure audit now proves streaming/non-streaming parity

继续推进完整 Agent Planner 目标，本轮把远端压力 smoke 的审计从“允许 streaming parity 缺证据”收紧为“必须证明”。

修正过程：

- 初次把 streaming calculator 直接调用 `_run_streaming_orchestration_scoped()` 时，runtime event 落到了 `workspace:unavailable`，这证明测试绕过了正式 workspace resolution，不能代表远端服务真实路径。
- 已改为通过正式 `run_streaming_orchestration()` 入口，并 patch `NativeProxyClient` 注入 fake upstream；这样 streaming final synthesis 使用同一个 `workspace_root` / tenant / session scope。

修改：

- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - 新增 `FakeStreamingHandler`。
  - admin tenant scope 下同时执行：
    - 非 streaming Gateway-owned `calculator` final synthesis；
    - streaming Gateway-owned `calculator` final synthesis。
  - `/admin/agent-runtime-audit.json` 现在要求 8 个 requirement 全部 `proven/current_scope`。
  - 新增输出 `admin_audit_streaming_parity_checked=true`。

验证：

```bash
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true

python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements -q
# 1 passed

python3 -m pytest -q
# 962 passed, 2 skipped, 21 warnings in 49.63s

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

### 2026-06-26 Audit now detects legacy gateway passthrough modes

继续做 completion audit 时发现一个旧 gateway 语义残留：`/admin/agent-runtime-audit.json` 之前只看 runtime events/memory/capability catalog，没有把当前 `gateway.tool_mode` 纳入结论。如果服务被配置成 `passthrough` / `native_passthrough` / `proxy`，它不应被认为是完整 Agent Planner Runtime。

修正：

- `src/gateway_http_handler.py`
  - `_agent_runtime_requirement_audit()` 新增 `runtime_config` 输入。
  - audit payload 新增 `runtime_config.gateway_tool_mode`、`runtime_config.upstream_tools_enabled`、`legacy_gateway_passthrough`。
  - 新增 requirement：`agent_planner_runtime_mode`。
  - 若 `gateway.tool_mode` 属于 `passthrough/native_passthrough/proxy`，该 requirement 为 `missing/current_scope`，整体状态不能为 `proven/current_scope`。
- `tests/test_gateway.py`
  - scoped audit 正常路径要求 `agent_planner_runtime_mode=proven/current_scope`。
  - 新增 `test_admin_agent_runtime_audit_flags_legacy_passthrough_mode`，证明 legacy passthrough 会被 audit 标红。
- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - 远端压力 smoke 的 required keys 增加 `agent_planner_runtime_mode`，并要求全部 requirement 均为 `proven/current_scope`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode -q
# 2 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true
```

最终验证补充：

```bash
python3 -m pytest -q
# 963 passed, 2 skipped, 21 warnings in 50.28s

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

### 2026-06-26 Audit now detects upstream native tool authority

继续做 completion audit 时发现第二个旧兼容缺口：即使 `gateway.tool_mode=orchestrate`，如果 upstream 被配置为 `tools_enabled=auto/native` 且 `supports_tools=true`、`supports_function_calls=true`，系统会把上游视为 native-tool capable。这不符合“chat-only upstream 只处理对话内容，工具权限属于 Agent Planner”的目标。

修正：

- `src/gateway_http_handler.py`
  - audit `runtime_config` 新增：
    - `upstream_supports_tools`
    - `upstream_supports_function_calls`
    - `upstream_native_tool_authority`
  - 新增 requirement：`chat_only_upstream_config`。
  - 如果 upstream 当前配置会授予 native tool/function-call authority，则 `chat_only_upstream_config=missing/current_scope`，整体状态不能为 `proven/current_scope`。
- `tests/test_gateway.py`
  - 正常 scoped audit 要求 `chat_only_upstream_config=proven/current_scope`。
  - 新增 `test_admin_agent_runtime_audit_flags_upstream_native_tool_authority`，证明 native-capable upstream 配置会被 audit 标红。
- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - 远端压力 smoke required keys 增加 `chat_only_upstream_config`，并确认 `upstream_native_tool_authority=false`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_upstream_native_tool_authority -q
# 3 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true
```

最终验证补充：

```bash
python3 -m pytest -q
# 964 passed, 2 skipped, 21 warnings in 50.98s

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

### 2026-06-26 Audit now detects Gateway-side user-machine tool execution

继续 completion audit 时发现第三个旧兼容 escape hatch：`execute_user_side_tools_in_gateway=true` 或 `delegate_tools_to_downstream=false` 会让 Gateway 服务侧执行 user-machine tools。这不符合远端服务边界：Read/Bash/Edit/Skill/GUI/local agent 必须在 downstream client workspace 执行。

修正：

- `src/gateway_http_handler.py`
  - audit `runtime_config` 新增：
    - `gateway_execute_user_side_tools`
    - `gateway_delegate_tools_to_downstream`
    - `gateway_forces_local_user_side_tools`
  - 新增 requirement：`downstream_client_tool_execution_policy`。
  - 如果 Gateway 被配置为服务侧执行 user-side tools，则该 requirement 为 `missing/current_scope`，整体不能为 `proven/current_scope`。
- `tests/test_gateway.py`
  - 正常 scoped audit 要求 `downstream_client_tool_execution_policy=proven/current_scope`。
  - 新增 `test_admin_agent_runtime_audit_flags_gateway_user_side_tool_execution`，证明即使存在 downstream tool_dispatch 事件，只要 config 允许 Gateway 执行 user-side tools，audit 仍标红。
- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - 远端压力 smoke required keys 增加 `downstream_client_tool_execution_policy`。
  - 确认 `gateway_forces_local_user_side_tools=false`。

验证：

```bash
python3 -m pytest tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_gateway_user_side_tool_execution \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_upstream_native_tool_authority -q
# 4 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true
```

最终验证补充：

```bash
python3 -m pytest -q
# 965 passed, 2 skipped, 21 warnings in 51.37s

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

### 2026-06-26 Final Agent Runtime completion audit gate

按原始目标做完整验收：将 Gateway 调整为远端外层 Agent Planner / Runtime；chat-only upstream 只做最终对话综合；Agent Planner/Runtime 负责 intent parsing、workflow/state machine、tool dispatch、client workspace 分派、Gateway-owned service tools、evidence compaction/injection、多轮状态与无限上下文 rollup/recall。

最终完整验收命令全部通过：

```bash
python3 -m pytest -q
# 965 passed, 2 skipped, 21 warnings in 51.35s

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1; project-analysis steps include update_plan, Skill, search_graph, get_code_snippet, trace_path, Read

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; upstream_calls=1; ignored_upstream_tool_attempt=Edit

python3 tests/integration/project_scope_cli_smoke.py
# pass=true; claude.ok=true; codex.ok=true; memory_service_root_leak=false

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_runtime_checked=true; admin_memories_checked=true; admin_events_checked=true; admin_audit_checked=true; admin_audit_streaming_parity_checked=true

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; rollups_checked=4; streaming_responses_recall_checked=4; compaction_checked=true; cross_tenant_leak_checked=true

git diff --check
# diff-check-pass

# bearer token literal audit excluding ignored runtime/local config files
# secret-literal-check-pass
```

Requirement-by-requirement evidence:

- Agent Planner mode, not legacy gateway: `/admin/agent-runtime-audit.json` includes `agent_planner_runtime_mode`; passthrough/proxy regression flags `missing/current_scope`.
- Chat-only upstream config: audit includes `chat_only_upstream_config`; native upstream tool authority regression flags `missing/current_scope`.
- User-machine tools in downstream client workspace: audit includes `downstream_client_tool_execution_policy`; Gateway-side user-tool execution regression flags `missing/current_scope`; project scope smoke proves no service-root memory leak.
- Planner owns intent/workflow/dispatch: project-analysis and remote pressure smokes prove deterministic planner steps and `tool_dispatch` before one final upstream call.
- Gateway-owned service tools: remote pressure smoke triggers Gateway-owned calculator and records service-side events.
- Infinite context: long-context smoke proves periodic rollup, streaming Responses recall, compaction, and cross-tenant isolation.
- Streaming/non-streaming parity: remote pressure smoke proves both `source=non_streaming` and `source=streaming` chat-only synthesis boundary in the same scoped runtime audit.
- Operator observability: admin runtime/memory/events/audit endpoints are checked under scoped tenant/workspace/session filters.

### 2026-06-26 Live test regression: block chat-only upstream refusal leakage

状态：已修复并重启本地 gateway。

用户在真实 Claude/Codex 客户端测试中发现：`分析这套项目` 已被 Agent Planner 识别为 `project_analysis`，但最终回答仍透传 chat-only upstream 的通用拒答：`Hello, I can't answer this question for now. Let's talk about something else.` 这说明运行时虽然进入 planner/synthesis boundary，但 final synthesis 缺少服务端兜底，不能把上游闲聊拒答当作 agent runtime 结果。

修正：

- `src/gateway_agent_planner.py`
  - 新增 `apply_synthesis_refusal_fallback()`。
  - 只在 `gateway_context.strategy=agent_planner_final_synthesis` / planner-owned final turn 生效。
  - 检测 `can't answer / cannot answer / let's talk about something else / 无法回答 / 换个话题` 等通用拒答。
  - 对 Anthropic Messages、OpenAI Chat Completions、OpenAI Responses 三种响应格式生成确定性 planner evidence 兜底回答。
- `src/gateway_tool_runtime.py`
  - non-streaming chat-only synthesis active 时，在 attach context 之前应用拒答兜底。
- `src/gateway_streaming.py`
  - streaming chat-only synthesis active 时同样应用拒答兜底，保证 streaming/non-streaming parity。
- `tests/test_gateway.py`
  - 新增 `test_agent_planner_does_not_leak_chat_only_refusal_after_evidence`。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py
# pass

python3 -m pytest -q tests/test_gateway.py -k 'chat_only or agent_planner_injects_compact_evidence or agent_planner_does_not_leak_chat_only_refusal'
# 11 passed, 278 deselected

./scripts/mimo_gateway.sh restart
curl -sS http://127.0.0.1:8885/healthz | python3 -m json.tool
# ok=true; mode=orchestrate; fake_prompt_tools=false
```

后续关注：如果 downstream client 返回的 Read/Bash evidence 全是错误，planner 仍应明确报告工具失败或重新请求下游工具，不能让坏证据长期卡在 synthesis；本次先修复确定的上游拒答泄漏。

### 2026-06-26 Strict Agent Planner every-turn mode

状态：已实现，live `.gateway_service.json` 已开启。

用户追加要求：每一个沟通都必须严格匹配 Agent Planner，不能只在项目分析/工具调用时才进入 planner。

修正：

- `src/gateway_agent_planner.py`
  - 新增 `strict_agent_planner_every_turn()`。
  - `prepare_upstream_body()` 在 strict 模式下对所有 chat/completions、responses、messages 注入 Agent Planner evidence/envelope。
  - 普通聊天也会被 Gateway-owned classifier 归类为 `plain_chat -> chat_only_synthesis`，并写入 planner state/runtime events。
  - Responses `input: string` 的 planner envelope 写入 `instructions`，不污染原始 user input。
  - Anthropic Messages 顶层 `system` 的 planner envelope 追加到 system 字段，避免插入非法/错序 system role。
- `src/gateway_tool_runtime.py`
  - `chat_only_synthesis_boundary` 与 `agent_planner` context 解耦。
  - strict 模式：所有沟通都进入 chat-only final synthesis boundary 并 strip tools。
  - 兼容模式：只有已有 planner evidence 或 Gateway-owned final synthesis 时才 strip tools，保留 legacy/native/text tool loop 功能。
- 配置：
  - live `.gateway_service.json`: `gateway.agent_planner_strict_every_turn=true`。
  - tracked sample `gateway.config.json/yaml`: 默认为 false，避免旧本地编排测试被 strict 模式误杀；生产/远端服务应开启。
- `tests/test_gateway.py`
  - 新增 strict plain chat 覆盖：普通 `hi` 也必须有 `gateway_context.agent_planner.intent.kind=plain_chat`，上游 tools 被 strip，runtime events 有 `intent_classification` 和 `chat_only_synthesis_boundary`。

验证：

```bash
python3 -m py_compile src/gateway_agent_planner.py src/gateway_tool_runtime.py src/gateway_streaming.py
# pass

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 967 passed, 2 skipped, 21 warnings
```

设计结论：Agent Planner envelope 与 legacy tool orchestration 需要分层。严格远端服务打开 `agent_planner_strict_every_turn`，保证每个沟通由 planner 分类并进入 chat-only synthesis；兼容模式保留全部旧功能测试。

### 2026-06-27 Live regression: ignore client-injected reminders during planner intent classification

状态：已修复，待 live service restart 后由真实客户端复测。

用户真实客户端暴露的问题：发送极短输入 `jo` 时，Gateway Agent Planner 派发了 `Bash` test runner；随后 `分析这套项目` 的 final synthesis 仍可能被 chat-only upstream 拒答污染。日志与 `gateway_log.sqlite3` 请求 ID 6148-6151 证明：客户端把 `<system-reminder>`、`SessionStart`、`PreToolUse` hook、全局 `CLAUDE.md/AGENTS.md` 内容作为 `role=user` 或 `role=system` 注入，planner 把其中的 “Run lint/typecheck/tests” 误判成当前用户意图。

修正：

- `src/gateway_agent_planner.py`
  - 新增 client-injected context sanitizer，过滤 `<system-reminder>`、`<context_guidance>`、`PreToolUse/PostToolUse`、`SessionStart`、`UserPromptSubmit`、CLAUDE/AGENTS 注入说明等。
  - `_planner_user_text()` 改为只取真实用户可见文本，跳过 tool_result 与 client runtime 注入块。
  - `_planner_conversation_text()` 不再在 structured messages 存在但可见文本为空时 fallback 到 raw JSON，避免 raw JSON 中的 hook 文本触发 validation/test workflow。
  - `prepare_upstream_body()` 改用 `_planner_conversation_text()`，不再用 `_conversation_text()` 原始 JSON 做 intent classification。
- `tests/test_gateway.py`
  - 新增 `test_agent_planner_ignores_client_injected_user_reminders_for_intent`，复现 `<system-reminder>` 中包含 “Run lint/typecheck/tests” 时仍应归类为 `plain_chat -> chat_only_synthesis`，不能派发 Bash。

验证：

```bash
python3 -m pytest -q tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_chat_only_refusal_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_client_injected_user_reminders_for_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_plain_chat_is_wrapped_by_agent_planner_envelope
# 3 passed

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 54.01s
```

### 2026-06-27 Audit correction: in-progress tool-dispatch sessions are strict-planner covered

状态：已修复并通过回归。

live audit 发现：`分析这套项目` 第一轮/中间轮次正常停在 `tool_dispatch`，但 `strict_every_turn_planner_envelope` 旧审计逻辑强制要求每个 scoped session 都已经有 `chat_only_synthesis_boundary`，导致进行中的 `project_analysis` 会话被误报 missing。

修正：

- `src/gateway_http_handler.py`
  - strict audit 覆盖条件从 `intent_classification + chat_only_synthesis_boundary` 调整为 `intent_classification + planner-owned boundary`。
  - planner-owned boundary 包括：final `chat_only_synthesis_boundary`、downstream `tool_dispatch`、Gateway-owned `gateway_tool_execute/gateway_tool_result`。
  - audit detail 新增 `synthesis_session_count` 与 `dispatch_session_count`，区分最终合成会话和进行中的工具派发会话。

验证：

```bash
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_non_strict_every_turn_mode \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_legacy_passthrough_mode \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_upstream_native_tool_authority \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_gateway_user_side_tool_execution \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_client_injected_user_reminders_for_intent
# 6 passed

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 52.72s
```

### 2026-06-27 Remote pressure smoke strict every-turn coverage

状态：已修复并通过。

继续按完整目标推进时，重新运行 Agent Planner integration smokes。`agent_planner_remote_pressure_smoke.py` 首次失败不是 runtime 不工作，而是 smoke 的 expected audit requirement keys 没同步新增的 `strict_every_turn_planner_envelope`；加入该 key 后又发现临时 pressure config 没开启 `gateway.agent_planner_strict_every_turn`，导致 strict requirement 在压力测试 scope 下不能 proven。

修正：

- `tests/integration/agent_planner_remote_pressure_smoke.py`
  - audit required keys 加入 `strict_every_turn_planner_envelope`。
  - 临时远端压力配置设置 `cfg["gateway"]["agent_planner_strict_every_turn"] = True`，让该 smoke 真正覆盖远端 strict 模式，而不是兼容模式。

验证：

```bash
python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1; final_text=PROJECT-ANALYSIS-FINAL-OK

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; ignored_upstream_tool_attempt=Edit

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; rollups_checked=4; streaming_responses_recall_checked=4; compaction_checked=true; cross_tenant_leak_checked=true

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; planner_sessions_checked=6; memory_rollups_checked=6; recall_payloads_checked=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true
```

### 2026-06-27 Live protocol strict planner smoke

状态：已验证。

在 live `http://127.0.0.1:8885` 上重新验证 OpenAI Chat、OpenAI Responses、Anthropic Messages 兼容路径，以及 streaming 路径。

验证结果：

- `/v1/chat/completions` non-stream：`gateway_context.agent_planner_strict_every_turn=true`，`intent.kind=plain_chat`，`strategy=agent_planner_final_synthesis`。
- `/v1/responses` non-stream：同上。
- `/v1/messages` non-stream：同上。
- `/v1/chat/completions` stream：SSE 有 `data:` 且完成。
- `/v1/responses` stream：SSE 有 `data:/event:` 且完成。
- scoped audit：
  - `strict_every_turn_planner_envelope=proven/current_scope`
  - `session_count=5`
  - `covered_session_count=5`
  - `missing_session_count=0`
  - `streaming_nonstreaming_parity=proven/current_scope`
  - `seen_synthesis_sources=[non_streaming, streaming]`

安全检查：

- `git diff --check` 通过。
- secret literal audit 发现真实 `sk-<redacted-live-key>` 只在 ignored 本地文件：`gateway_log.sqlite3`、`.gateway_service.json`、`.case.txt`、`.ruff_cache/`。
- tracked 文件中只有假 key 示例：`tests/test_stats_logging.py` 的 `sk-secret-key-12345` 与 docs archive 的 `sk-xxx`。

### 2026-06-27 Strict protocol smoke persisted

状态：已新增并通过。

为避免只依赖手工 live curl 证明“每个沟通都严格匹配 Agent Planner”，新增可复跑集成 smoke：

- `tests/integration/agent_planner_protocol_strict_smoke.py`
  - 启动 fake OpenAI-chat upstream 与本地 Gateway HTTP server。
  - 临时配置强制 `gateway.agent_planner_strict_every_turn=true`、`tools_enabled=adapter`、`supports_tools=false`、`supports_function_calls=false`。
  - 覆盖 non-stream：`/v1/chat/completions`、`/v1/responses`、`/v1/messages`。
  - 覆盖 stream：`/v1/chat/completions`、`/v1/responses`、`/v1/messages`。
  - 断言每个 upstream payload 都有 `gateway_context.agent_planner_strict_every_turn=true`、`chat_only_synthesis=true`，且没有 `tools/tool_choice` 泄漏到 chat-only upstream。
  - 查询 `/admin/agent-runtime-audit.json`，断言 `strict_every_turn_planner_envelope=proven/current_scope`、`covered_session_count=6`、`missing_session_count=0`、`streaming_nonstreaming_parity=proven/current_scope`。

验证：

```bash
python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; non_stream_paths=[chat,responses,messages]; stream_paths=[chat,responses,messages]; upstream_calls=6; covered_session_count=6

python3 tests/integration/agent_planner_protocol_strict_smoke.py && python3 tests/integration/agent_planner_remote_pressure_smoke.py
# both ok=true

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 52.66s
```

### 2026-06-27 Strict Agent Planner semantic-cache bypass fix

状态：已修复、已验证，live 8885 服务仍需重启加载本次代码。

用户真实测试继续暴露“看起来有 planner，但真实客户端仍不稳定”的问题。本轮复盘 `gateway_log.sqlite3` 与 strict protocol smoke，确认一个结构性缺口：HTTP handler 在非流式、无 tools 的请求上会先查 semantic cache；命中后直接返回旧响应，绕过 `run_tool_orchestration()`，因此不会为这个 exact turn 产生 intent/session/workspace/audit envelope。扩展后的 `agent_planner_protocol_strict_smoke.py` 覆盖 `/anthropic/v1/*` alias 后首次失败：`upstream call count drifted: 9`，说明 alias non-stream 请求被 cache 命中而没有进入 chat-only upstream/planner boundary。

修正：

- `src/gateway_http_handler.py`
  - 在 `gateway.agent_planner_strict_every_turn=true` 时禁用 HTTP 层 semantic cache read/write path。
  - 原因：strict remote Agent Planner 模式要求每次沟通都必须独立经过 planner，记录 intent、tenant/session/workspace isolation、runtime event、chat-only synthesis/tool-dispatch boundary；缓存旧响应会携带旧 path/session 的 planner context，破坏 every-turn 语义。

验证：

```bash
python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; canonical + /anthropic/v1 alias; upstream_calls=12; session_count=12; covered_session_count=12; missing_session_count=0; seen_synthesis_sources=[non_streaming, streaming]

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; streaming parity checked

python3 -m py_compile src/gateway_http_handler.py tests/integration/agent_planner_protocol_strict_smoke.py
python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# all ok=true

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 50.46s
```

结论：strict every-turn 模式下，semantic cache 不再能绕过远端 Agent Planner。canonical 与 `/anthropic/v1/*`，stream 与 non-stream 都必须进入 planner/audit 后才返回。

Live reload verification after semantic-cache bypass fix:

```text
./scripts/mimo_gateway.sh restart
# restarted pid on 8885; /healthz ok=true, mode=orchestrate

/v1/messages?beta=true with injected system-reminder + text "jo" + declared Bash tool:
- stop_reason=end_turn
- content block types=[thinking,text]
- tool_use emitted=false
- intent.kind=plain_chat
- intent.workflow=chat_only_synthesis
- intent.signals=[declared_tools,no_tool_intent]

/v1/messages?beta=true with "分析这套项目" + Bash/Read tools:
- stop_reason=tool_use
- content block types=[tool_use]
- tool_names=[Bash]
- planner dispatched downstream client tool instead of chat-only refusal

/admin/agent-runtime-audit.json?tenant_contains=<live-run>:
- strict_every_turn_planner_envelope=proven/current_scope
- session_count=2
- covered_session_count=2
- synthesis_session_count=1
- dispatch_session_count=1
- missing_session_count=0
```

Note: the small live sample's overall audit remains `needs_runtime_evidence` only because it did not exercise memory rollup or streaming parity; those are covered by `agent_planner_remote_pressure_smoke.py` and `agent_planner_long_context_pressure_smoke.py` above.

### 2026-06-27 Tool-dispatch response intent visibility fix

状态：已修复、已验证、live 8885 已重启加载。

继续按“每一个沟通都必须严格匹配 Agent Planner”审计 live 结果时发现：`分析这套项目` 已正确进入 planner 并返回 downstream `tool_use`，但响应中的 `gateway_context.agent_planner.intent` 顶层为空；intent 只存在于 `gateway_context.agent_planner.state.intent`。这不影响内部 dispatch/audit，但影响客户端和远端日志的直接可观测性，容易被误判为“这轮没有匹配 Agent Planner”。

修正：

- `src/gateway_tool_runtime.py`
  - `_direct_downstream_tool_request_response()` 在构造 `gateway_context.agent_planner` 时，把 `planner_state_snapshot(...).intent` 提升到顶层 `agent_planner.intent`。
- `tests/test_gateway.py`
  - `test_agent_planner_parallel_users_keep_intent_and_workspace_isolated` 增加响应级断言：两个并发用户的 downstream tool-dispatch 响应都必须带顶层 `intent.kind=read_file`、`intent.workflow=generic_tool`。

验证：

```bash
python3 -m pytest -q tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_client_injected_user_reminders_for_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_chat_only_refusal_after_evidence
# 3 passed

python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_protocol_strict_smoke.py
python3 tests/integration/agent_planner_remote_pressure_smoke.py
# all ok=true; strict protocol upstream_calls=12; remote pressure users=6

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 50.53s
```

Live reload verification:

```text
./scripts/mimo_gateway.sh restart
/v1/messages?beta=true: "分析这套项目" with Bash/Read tools
- stop_reason=tool_use
- tool_names=[Bash]
- gateway_context.agent_planner.intent.kind=project_analysis
- gateway_context.agent_planner.intent.workflow=project_analysis
- state.intent matches top-level intent
```

### 2026-06-27 Streaming tool-dispatch intent regression lock

状态：已验证并补测试；live 8885 已通过真实 SSE 验证。

继续审计“每一个沟通都必须严格匹配 Agent Planner”时，上一轮已修复 non-stream downstream `tool_use` 响应顶层 `gateway_context.agent_planner.intent`。本轮确认 streaming 路径复用同一个 direct downstream response，并会在 Anthropic SSE `message_delta.gateway_context` 中输出顶层 planner intent；为避免未来退化，补充 regression 断言。

修正/测试：

- `tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool`
  - 新增断言 SSE 包含 `gateway_context`。
  - 新增断言顶层 `intent.kind=shell_command`。
  - 新增断言 `workflow=generic_tool`。

验证：

```bash
python3 -m pytest -q tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_direct_file_read_surfaces_downstream_tool_without_upstream \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated
# 3 passed

python3 tests/integration/agent_planner_protocol_strict_smoke.py && python3 tests/integration/agent_planner_remote_pressure_smoke.py
# both ok=true; strict protocol upstream_calls=12; remote pressure users=6

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 969 passed, 2 skipped, 21 warnings in 50.93s
```

Live SSE verification:

```text
/v1/messages?beta=true stream=true
user: Run bash command `printf STREAM-BASH-OK` and reply only with stdout.
- SSE content_block_start: type=tool_use, name=Bash
- partial_json: {"command":"printf STREAM-BASH-OK"}
- message_delta.gateway_context.agent_planner.intent.kind=shell_command
- message_delta.gateway_context.agent_planner.intent.workflow=generic_tool
- admin audit strict_every_turn_planner_envelope=proven/current_scope
- audit detail: session_count=1, covered_session_count=1, synthesis_session_count=0, dispatch_session_count=1, missing_session_count=0
```

注意：第一次 live 验证失败是测试脚本 heredoc 未加单引号，shell 在发送 JSON 前展开了反引号，实际发给服务的是 `Run bash command STREAM-BASH-OK...`，不是带命令块的请求。已用 single-quoted heredoc 复测通过。

### 2026-06-27 Gateway-owned Assistants/Threads + client workspace metadata fix

状态：已修复、已重启 live 8885、已通过 live curl matrix 与全量 pytest。

本轮根据 live 服务失败继续审计，发现两个实际问题：

1. `healthz.supported_paths` 宣称支持 `/v1/assistants`、`/v1/threads`，但请求被当作 chat 请求转发给弱上游，先因为 `UpstreamHTTPError` 构造参数错误返回 500，修正后仍因上游 schema 不支持返回 502。
2. 远端多用户请求如果用 `metadata.workspace` 表示客户端 workspace，旧解析不识别，会退回服务端 `GATEWAY_WORKSPACE_ROOT`，不符合“目录是 client 自己 workspace”。

修正：

- `src/gateway_proxy.py` / `src/gateway_errors.py`
  - 修复 curl transport 的 upstream 4xx 错误构造：不再重复传 `upstream_status`。
- `src/gateway_assistants.py`
  - 新增 Gateway-owned exact endpoint 兼容层：`POST /v1/assistants` 返回 assistant object，`POST /v1/threads` 返回 thread object，不再依赖 chat-only upstream 是否实现 Assistants API。
- `src/gateway_http_handler.py`
  - 在进入 tool orchestration / upstream forwarding 前先处理 Gateway-owned assistants/threads base endpoints，并记录 request stats/log。
- `src/gateway_tool_runtime.py`
  - workspace 解析新增 `metadata.workspace`、`metadata.workspace_dir`、top-level `workspace`，优先于服务端 env var。
- `tests/test_gateway_assistants.py`、`tests/test_gateway_proxy_errors.py`
  - 增加 assistants/threads gateway-owned、upstream error preservation、workspace metadata priority 回归测试。

Live 证据：

```text
GET  /v1/models                         -> 200 object=list
POST /v1/messages/count_tokens          -> 200
POST /v1/chat/completions/count_tokens  -> 200
POST /v1/assistants                     -> 200 object=assistant
POST /v1/threads                        -> 200 object=thread
POST /v1/tools/call                     -> 200 object=gateway.tool_result
POST /v1/functions/call                 -> 200 object=gateway.tool_result
```

`jo` + client injected `system-reminder` + declared `Bash` tool live proof：

```text
stop_reason=end_turn
content_types=[thinking, text]
tool_names=[]
intent.kind=plain_chat
workflow=chat_only_synthesis
session_key=/v1/messages:/Users/sanbo/Desktop/ti:tenant:live-jo-sanitizer-014034:session_id:live-jo-sanitizer-014034-session
workspace log=Workspace resolved via [session_metadata]: /Users/sanbo/Desktop/ti
strict_every_turn_planner_envelope=proven/current_scope
covered_session_count=1
missing_session_count=0
```

验证：

```bash
python3 -m pytest -q tests/test_gateway_assistants.py tests/test_gateway_proxy_errors.py \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_client_injected_user_reminders_for_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_parallel_users_keep_intent_and_workspace_isolated \
  tests/test_gateway.py::AnthropicSSEFormatTests::test_streaming_adapter_explicit_bash_request_surfaces_downstream_tool
# 9 passed

python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; upstream_calls=12; covered_session_count=12; missing_session_count=0

python3 tests/integration/agent_planner_remote_pressure_smoke.py
# ok=true; users=6; admin_audit_checked=true; admin_audit_streaming_parity_checked=true

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 975 passed, 2 skipped, 21 warnings in 51.94s
```

补充 live 项目分析复测：

```text
/v1/messages?beta=true user="分析这套项目" with Bash/Read tools
stop_reason=tool_use
tool_uses=[Bash]
intent.kind=project_analysis
intent.workflow=project_analysis
session_key=/v1/messages:/Users/sanbo/Desktop/ai_tool_functioncall:tenant:live-project-analysis-014326:session_id:live-project-analysis-014326-session
strict_every_turn_planner_envelope=proven/current_scope
dispatch_session_count=1
missing_session_count=0
```

### 2026-06-27 Advertised public surface smoke locked

状态：已新增离线集成 smoke，并通过。

为避免再次出现 `healthz.supported_paths` 宣称支持但实际请求 500/502，本轮新增：

- `tests/integration/agent_planner_public_surface_smoke.py`

该 smoke 启动 fake OpenAI-chat upstream 与本地 Gateway HTTP server，开启 `gateway.agent_planner_strict_every_turn=true`，然后读取 `/healthz.supported_paths` 并逐条请求全部 advertised paths，包括 canonical 与 `/anthropic/v1/*` aliases。

覆盖范围：

```text
/anthropic/v1/assistants
/anthropic/v1/chat/completions
/anthropic/v1/chat/completions/count_tokens
/anthropic/v1/functions/call
/anthropic/v1/messages
/anthropic/v1/messages/count_tokens
/anthropic/v1/models
/anthropic/v1/responses
/anthropic/v1/threads
/anthropic/v1/tools/call
/tools/call
/v1/assistants
/v1/chat/completions
/v1/chat/completions/count_tokens
/v1/functions/call
/v1/messages
/v1/messages/count_tokens
/v1/models
/v1/responses
/v1/threads
/v1/tools/call
```

验证结果：

```text
python3 tests/integration/agent_planner_public_surface_smoke.py
# ok=true
# advertised_count=21
# every advertised path status=200
# upstream_calls=6
# strict_sessions.covered_session_count=6
# strict_sessions.missing_session_count=0
```

这条 smoke 现在是“每一个 health advertised 功能必须可调用”和“conversation paths 必须严格进入 Agent Planner”的回归闸门。

补充加严：public surface smoke 现在不仅验证 direct tool/function endpoints 可调用，还验证它们不会误用服务端 workspace。

实现方式：

- smoke 在 run dir 下同时创建：
  - `service-workspace/surface-client.txt` = `SERVICE_WORKSPACE_SHOULD_NOT_BE_USED`
  - `client-workspace/surface-client.txt` = `CLIENT_WORKSPACE_OK`
- `/anthropic/v1/functions/call`、`/anthropic/v1/tools/call`、`/tools/call`、`/v1/functions/call`、`/v1/tools/call` 全部使用 `metadata.workspace=<client-workspace>` 调 `Read(surface-client.txt)`。
- 断言 direct tool result content 必须包含 `CLIENT_WORKSPACE_OK`，且不能包含 `SERVICE_WORKSPACE_SHOULD_NOT_BE_USED`。

验证输出中 direct endpoint 均显示：

```text
Workspace resolved via [session_metadata]: .../client-workspace
status=200
kind=direct_tool
```

### 2026-06-27 Direct tool/function runtime audit boundary

状态：已修复并验证。

继续按“每一个沟通都需要严格匹配 Agent Planner / Runtime 可证明边界”审计时发现：direct tool/function endpoints 已可调用且 workspace 正确，但此前只返回工具结果，没有写入 Agent Runtime event。这样 operator 只能从 request log 看到 direct endpoint，不能从 `/admin/agent-runtime-events.json` 或 requirement audit 证明这类非聊天请求的执行边界。

修正：

- `src/gateway_tool_runtime.py::execute_direct_tool_call()`
  - 新增 `path` 参数，默认 `/tools/call`。
  - 在 client workspace scope 内记录：
    - `direct_tool_execute`
    - `direct_tool_result`
  - metadata 标记 `source=direct_tool_endpoint`、`owner=gateway_service`、`tool_names`、`success`。
- `src/gateway_http_handler.py`
  - direct tool/function HTTP 路由调用 `execute_direct_tool_call(body, path=path)`，保留 canonical path 到 runtime session key。
  - requirement audit 的 `gateway_owned_service_tools` 现在把 `direct_tool_execute/direct_tool_result` 也纳入证据。
- `tests/integration/agent_planner_public_surface_smoke.py`
  - 增加 admin events 断言：5 条 direct public paths 都产生 `direct_tool_result` event。
  - 增加 audit 断言：`gateway_owned_service_tools=proven/current_scope`。
  - 验证 direct events 的 `workspace_key` 指向 client workspace。

验证：

```text
python3 tests/integration/agent_planner_public_surface_smoke.py
# advertised_count=21
# direct_tool_result_event_count=5
# direct_tool_event_paths=[/tools/call, /v1/functions/call, /v1/tools/call]
# gateway_owned_service_tools=proven/current_scope
# strict_sessions.missing_session_count=0
```

### 2026-06-27 Direct tool/function invalid-input error boundary

状态：已修复并验证。

继续审计 direct tool/function 失败路径时发现：缺失 tool/function name 会抛 `ToolExecutionError`，但该异常不是 `GatewayError`，HTTP handler 会按 fallback 返回 500。这不符合 public API 稳定性，也无法证明失败请求进入 Runtime 边界。

修正：

- `src/gateway_errors.py`
  - 新增 `BadRequestError(status=400)`。
- `src/gateway_tool_runtime.py::execute_direct_tool_call()`
  - 捕获 direct-call parse 阶段的 `ToolExecutionError`。
  - 记录 `direct_tool_error` runtime event。
  - 返回结构化 400：`detail.failure_type=invalid_input`。
- `tests/integration/agent_planner_public_surface_smoke.py`
  - 增加 invalid direct request：`POST /v1/tools/call` 缺失 tool/function name。
  - 断言 HTTP 400。
  - 断言 `direct_tool_error` event 数量为 1，`failure_type=invalid_input`，workspace 仍是 client workspace。

验证：

```text
invalid direct request -> HTTP 400
detail.failure_type=invalid_input
direct_tool_error_event_count=1
workspace_key contains client-workspace
```

### 2026-06-27 Tool result cache workspace isolation fix

状态：全量验证中发现并修复。

在 direct invalid-input 修复后跑全量 pytest，暴露 `test_parallel_direct_tool_calls_keep_client_workspaces_isolated` 偶发失败：多个并发 client workspace 同时 `Read(marker.txt)` 时，某个请求会拿到另一个 workspace 的内容。

根因：`_execute_tool_call()` 的 tool result cache 对 cacheable read-only tools 只按 `tool_name + arguments` 缓存。不同 remote client workspace 中相同参数（例如 `Read({file_path: marker.txt})`）会共享缓存，导致跨 workspace 污染。

修复：

- `src/gateway_tool_runtime.py`
  - tool result cache 的 get/put 使用单独的 `_tool_cache_arguments`。
  - `_tool_cache_arguments` 加入 `__gateway_workspace_cache_key=str(_workspace_root())`。
  - sentinel 只参与缓存 key，不传给实际 tool handler。

验证：

```text
for i in $(seq 1 30); do python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_parallel_direct_tool_calls_keep_client_workspaces_isolated; done
# parallel_direct_30x_ok
```

这修复了远端多用户并发 workspace 隔离的一个真实风险点。

### 2026-06-27 metadata.tenant 多租户隔离修复

状态：已修复、live 验证、回归测试通过。

用户 live 测试暴露后继续查 runtime/audit，发现一个真实远端服务缺口：请求里带 `metadata.tenant` 时，Gateway 只识别 `tenant_id/account_id/organization_id/user_id/user`，没有识别 `tenant`。结果是：

- `metadata.workspace` 能正确进入 client workspace；
- 但 planner/runtime events 的 `tenant_key` 仍可能是 `anonymous`；
- `/admin/agent-runtime-audit.json?tenant_contains=...` 查不到该用户请求；
- 多用户并发下 audit/隔离证据不可靠。

修复：

- `src/gateway_agent_planner.py`：Planner session key tenant 解析接受 `metadata.tenant` 和嵌套 `user_id.tenant`。
- `src/gateway_context.py`：memory session key tenant 解析接受 `metadata.tenant`。
- `src/gateway_tool_runtime.py`：匿名隔离 workspace 与 runtime scope tenant 解析接受 `metadata.tenant`。
- `gateway.config.json` / `gateway.config.yaml` / `src/gateway_config.py`：strict every-turn 默认改为 true，避免新配置退回兼容模式。

Live 证明：

```text
/v1/messages?beta=true metadata.tenant=live-tenant-alias-user metadata.workspace=/Users/sanbo/Desktop/ti user=jo
stop_reason=end_turn
tool_names=[]
intent.kind=plain_chat
session_key=/v1/messages:/Users/sanbo/Desktop/ti:tenant:live-tenant-alias-user:session_id:tenant-alias-jo
admin events tenant_key=live-tenant-alias-user
strict_every_turn_planner_envelope=proven/current_scope
missing_session_count=0
```

回归验证：

```text
python3 -m pytest -q \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_session_key_accepts_metadata_tenant_alias \
  tests/test_gateway.py::NativeGatewayTests::test_remote_anonymous_workspace_accepts_metadata_tenant_alias \
  tests/test_context_enhanced.py::TestMemorySessionKey::test_session_accepts_metadata_tenant_alias
# 3 passed

python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; upstream_calls=12; covered_session_count=12; missing_session_count=0

python3 tests/integration/agent_planner_public_surface_smoke.py
# ok=true; advertised_count=21; covered_session_count=6; missing_session_count=0; direct_tool_result_event_count=5
```

### 2026-06-27 count_tokens Runtime boundary

状态：已修复、public smoke 与 live audit 通过。

继续按“每一个公开沟通都必须进入 Agent Planner/Runtime 可证明边界”审计时发现：`/v1/messages/count_tokens`、`/v1/chat/completions/count_tokens` 及 `/anthropic/v1/*` aliases 虽然可调用并返回 `input_tokens`，但此前只写 request log，不写 Agent Runtime event。这样 public surface 里 count_tokens 功能不可由 `/admin/agent-runtime-events.json` / requirement audit 证明。

修复：

- `src/gateway_tool_runtime.py::token_count_response(body, path=...)`
  - 在 resolved client workspace scope 内记录：
    - `token_count_execute`
    - `token_count_result`
  - event metadata：`owner=gateway_service`、`source=token_count_endpoint`、`success=true`、`input_tokens`。
- `src/gateway_http_handler.py`
  - count_tokens HTTP 路由传入 canonical path。
  - requirement audit 的 `gateway_owned_service_tools` 纳入 `token_count_execute/token_count_result`。
- `tests/integration/agent_planner_public_surface_smoke.py`
  - count_tokens payload 加入 `metadata.workspace` / tenant。
  - 断言 4 个 count_tokens public paths 都产生 `token_count_result` event，且 workspace 是 client workspace。

Live 证明：

```text
POST /v1/messages/count_tokens metadata.tenant=live-token-count-user metadata.workspace=/Users/sanbo/Desktop/ti
response.input_tokens=54
event token_count_execute tenant_key=live-token-count-user workspace_key=/Users/sanbo/Desktop/ti
event token_count_result metadata.source=token_count_endpoint metadata.input_tokens=54
gateway_owned_service_tools=proven/current_scope
```

验证：

```text
python3 tests/integration/agent_planner_public_surface_smoke.py
# ok=true
# token_count_result_event_count=4
# direct_tool_result_event_count=5
# strict_sessions.missing_session_count=0

python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; 12/12 strict sessions covered
```

### 2026-06-27 models/assistants/threads Runtime boundary

状态：已修复、public smoke 与 live audit 通过。

继续审计 public surface 时发现：`/v1/models`、`/v1/assistants`、`/v1/threads` 及 `/anthropic/v1/*` aliases 已经可调用，但它们属于 Gateway-owned public API，之前没有统一写入 Agent Runtime event。这样 health advertised 的非聊天功能虽然能返回 payload，但 operator 无法从 Runtime/Audit 证明 tenant/workspace/session 边界。

修复：

- `src/gateway_tool_runtime.py`
  - 新增 `record_gateway_public_endpoint()`。
  - 记录 Gateway-owned public endpoint result/error event。
- `src/gateway_http_handler.py`
  - `/v1/models` GET 成功记录 `models_result`，失败可记录 `models_error`。
  - `/v1/assistants` 成功记录 `assistants_result`。
  - `/v1/threads` 成功记录 `threads_result`。
  - `gateway_owned_service_tools` audit 纳入这些事件。
- `tests/integration/agent_planner_public_surface_smoke.py`
  - models GET 使用 query metadata 提供 tenant/workspace/session，不改变响应 payload。
  - 断言：
    - `models_result_event_count=2`
    - `assistants_result_event_count=2`
    - `threads_result_event_count=2`
    - 这些事件都 scoped 到 client workspace。

Live 证明：

```text
GET /v1/models?tenant=live-public-owned-user&workspace=/Users/sanbo/Desktop/ti&session_id=live-models
models_result tenant_key=live-public-owned-user workspace_key=/Users/sanbo/Desktop/ti model_count=6

POST /v1/assistants metadata.tenant=live-public-owned-user metadata.workspace=/Users/sanbo/Desktop/ti
assistants_result tenant_key=live-public-owned-user workspace_key=/Users/sanbo/Desktop/ti object=assistant

POST /v1/threads metadata.tenant=live-public-owned-user metadata.workspace=/Users/sanbo/Desktop/ti
threads_result tenant_key=live-public-owned-user workspace_key=/Users/sanbo/Desktop/ti object=thread
```

验证：

```text
python3 tests/integration/agent_planner_public_surface_smoke.py
# ok=true
# advertised_count=21
# models_result_event_count=2
# assistants_result_event_count=2
# threads_result_event_count=2
# token_count_result_event_count=4
# direct_tool_result_event_count=5
# strict_sessions.missing_session_count=0

python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; 12/12 strict sessions covered
```

### 2026-06-27 models error Runtime boundary

状态：已修复并由 public surface smoke 覆盖。

继续审计 Gateway-owned public endpoint 的失败路径时发现：models 成功路径已记录 `models_result`，但缺少回归证明上游 models 失败时也会留下 Runtime error boundary。该路径很重要，因为 `/v1/models` 是客户端启动/能力探测的常用入口，失败时也必须可审计，而不能只返回 HTTP 502。

补强：

- `src/gateway_http_handler.py`
  - models upstream failure 分支调用 `record_gateway_public_endpoint(..., resource="models", success=False, failure_type=...)`。
- `tests/integration/agent_planner_public_surface_smoke.py`
  - fake upstream 新增 `PublicSurfaceUpstream.fail_models`。
  - 正常 public surface 全量检查后，强制 `/v1/models` 上游返回 503。
  - 断言 Gateway 返回 502。
  - 断言产生 `models_error` event，且：
    - `owner=gateway_service`
    - `success=false`
    - `failure_type` 存在
    - `workspace_key` 为 client workspace。

验证：

```text
python3 tests/integration/agent_planner_public_surface_smoke.py
# ok=true
# models_result_event_count=2
# models_error_event_count=1
# gateway_owned_service_tools=proven/current_scope
# strict_sessions.missing_session_count=0
```


---

## 2026-06-27 Agent Planner client-context poisoning 修复

**状态**: 已修复并已在 live `127.0.0.1:8885` 验证。

本轮针对真实客户端失败链：同一 session 中先发送带 `<system-reminder>` / `PreToolUse` / `SessionStart` 的短输入 `jo`，随后发送 `分析这套项目`。旧行为会把客户端注入上下文和 recalled memory 混入当前用户意图，导致第二轮被误判为 `plain_chat`，chat-only 上游直接合成“无法访问文件/换话题”类回答。

修复点：
- `src/gateway_agent_planner.py`：client 注入 sanitizer 先删除 `<system-reminder>...</system-reminder>`，保留块后的真实用户文本；最终 synthesis 上游消息也使用净化后的用户文本。
- `src/gateway_agent_planner.py`：recalled memory stripping 在 memory block 结束后保留当前用户行，避免把 `分析这套项目` 吞掉。
- `src/gateway_context.py`：注入 memory block 时把历史摘要压成单行，避免多行历史再次污染 planner。
- `tests/test_agent_planner_client_context.py`：新增回归，固定“injected jo -> same-session project analysis”必须返回 downstream `Bash` tool_use，且 chat-only upstream 不应看到 `PreToolUse` / `SessionStart`。

验证：
```bash
python3 -m pytest -q tests/test_agent_planner_client_context.py
# 1 passed

python3 -m pytest -q tests/test_gateway.py -k 'chat_only_project_analysis_without_path or chat_only_project_analysis_uses_declared_shell_tool or prefers_codebase_onboarding_skill'
# 3 passed, 291 deselected

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; upstream_calls=1

python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; strict covered_session_count=12; missing_session_count=0

python3 tests/integration/agent_planner_multiround_smoke.py
# ok=true; ignored_upstream_tool_attempt=Edit

python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# ok=true; users=4; cross_tenant_leak_checked=true

# live 8885 curl repro
# jo: stop=end_turn, tools=[]、intent=plain_chat
# 分析这套项目: stop=tool_use, tools=[Bash]、intent=project_analysis
```


### 2026-06-27: Agent Runtime audit 证据窗口修正

**问题**: `/admin/agent-runtime-audit.json` 的默认全局视图会把 durable planner session 表里的历史旧 session 与当前 event 窗口混合，导致已经修复后的 live 服务仍显示 `strict_every_turn_planner_envelope=missing/current_scope`。这些缺失来自旧 anonymous session 或截断证据窗口，不代表当前请求未进入 Agent Planner。

**修复**:
- audit endpoint 内部取证窗口默认提升到 `audit_limit=500`（仍严格应用 tenant/workspace/session filters，不扩大可见范围）。
- strict every-turn 审计只把当前过滤事件里存在 `intent_classification` 的 session 作为可审计候选；有 intent 但没有 synthesis/tool-dispatch/gateway-tool boundary 才算真正缺失。
- 未带 tenant/workspace/session filter 的全局视图对 `tenant_workspace_isolation` 只能给 `configured/static`，真正 runtime proof 必须使用 scoped filter 或 pressure smoke。

验证：
```bash
python3 -m pytest -q tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_ignores_stale_sessions_outside_event_window \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_proves_scoped_remote_requirements \
  tests/test_gateway.py::NativeGatewayTests::test_admin_agent_runtime_audit_flags_non_strict_every_turn_mode
# 3 passed

python3 tests/integration/agent_planner_remote_pressure_smoke.py
python3 tests/integration/agent_planner_public_surface_smoke.py
python3 tests/integration/agent_planner_protocol_strict_smoke.py
# all ok
```

## 2026-06-27 live 用户失败链复测：`jo` 后 `分析这套项目`

**状态**: 当前运行中的 `127.0.0.1:8885` 已按用户截图链路复测，截图里的错误行为在当前进程中未复现。

本次复测覆盖：
- Anthropic `/v1/messages?beta=true`：workspace=`/Users/sanbo/Desktop/ti`，同一 session 先 `jo` 再 `分析这套项目`。
- OpenAI `/v1/chat/completions`：同一 session 先 `jo` 再 `分析这套项目`。
- 工具结果回传：`分析这套项目` 第一轮返回 `Bash` 后，把 Bash 输出回传给 Gateway，Planner 继续发下一步 `Bash`，未掉到 chat-only upstream 拒答。

Live 结论：
```text
messages + /Users/sanbo/Desktop/ti:
  jo -> stop_reason=end_turn, content=[thinking,text], no tool_use
  分析这套项目 -> stop_reason=tool_use, tool=Bash, intent=project_analysis

chat/completions + /Users/sanbo/Desktop/ai_tool_functioncall:
  jo -> finish_reason=stop, tool_calls=null, intent=plain_chat
  分析这套项目 -> finish_reason=tool_calls, tool=Bash, intent=project_analysis

chat/completions full tool-result loop:
  project_structure Bash result returned -> next response finish_reason=tool_calls, tool=Bash(core_flow_trace)
```

同步验证：
```bash
python3 -m pytest -q tests/test_agent_planner_client_context.py \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_ignores_client_injected_user_reminders_for_intent \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_plain_chat_is_wrapped_by_agent_planner_envelope
# 3 passed

python3 tests/integration/agent_planner_project_analysis_smoke.py
python3 tests/integration/agent_planner_multiround_smoke.py
python3 tests/integration/agent_planner_remote_pressure_smoke.py
python3 tests/integration/agent_planner_public_surface_smoke.py
python3 tests/integration/agent_planner_protocol_strict_smoke.py
python3 tests/integration/agent_planner_long_context_pressure_smoke.py
# all ok on current live service
```

仍需注意：若客户端仍看到旧错误，优先确认它实际连到的是当前 `127.0.0.1:8885` 进程，而不是旧进程/旧配置/另一个 workspace endpoint；当前进程日志应出现对应 tenant/workspace 的 `intent_classification` 与 `tool_dispatch` 事件。

## 2026-06-27 全局 audit strict 误报修正

**状态**: 已修复并已在 live `127.0.0.1:8885` 验证。

发现的问题：全局 `/admin/agent-runtime-audit.json` 不带 tenant/workspace/session scope 时，会混入 durable 历史 session、anonymous workspace、旧 event window。部分历史 anonymous plain-chat session 只有 `intent_classification` / `planner_state`，缺少后来新增的 `chat_only_synthesis_boundary`，导致 global audit 把当前服务显示为 `strict_every_turn_planner_envelope=missing/current_scope`。

修正：
- `src/gateway_http_handler.py`：strict every-turn 的 **runtime proof** 只在带 `tenant_contains`、`workspace_contains` 或 `session_contains` 的 scoped audit 中执行。
- 无 scope 的 global operator view 不再把历史 anonymous session 作为当前严格模式失败；它返回 `configured/static`，并在 detail 中暴露：
  - `runtime_scope_required=true`
  - `strict_runtime_scope=false`
  - `unscoped_intent_session_count=<count>`
- scoped audit 仍保持严格：只要当前 scope 内有 `intent_classification` 但没有 `chat_only_synthesis_boundary` / `tool_dispatch` / gateway tool boundary，就会继续报 missing。
- `tests/test_gateway.py`：新增回归 `test_agent_runtime_audit_global_view_does_not_fail_on_unscoped_historical_anonymous_sessions`，并保留 scoped strict proof/failure 测试。

Live 验证：
```text
GET /admin/agent-runtime-audit.json?limit=120
  overall_status=partially_proven
  summary={proven:10, configured:2, missing:0, total:12}
  strict_every_turn_planner_envelope.status=configured/static
  strict_detail.strict_runtime_scope=false
  strict_detail.unscoped_intent_session_count=59
```

Scoped 验证仍为 runtime proof：
```bash
python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; covered_session_count=12; missing_session_count=0; strict_runtime_scope=true

python3 tests/integration/agent_planner_public_surface_smoke.py
# ok=true; advertised_count=21; strict missing_session_count=0; strict_runtime_scope=true
```

## 2026-06-27 Agent Runtime scope contract 补齐

**状态**: 已实现并在 live audit 验证。

为避免“每一个沟通都要进 Agent Planner”被误解成 admin 控制面、未认证请求、未知路径也要创建用户 planner session，本轮把 scope 规则做成机器可读 audit contract：

- `scope_contract.strict_conversation_scope=supported_authenticated_public_api_paths`
- `conversation_paths`：所有 authenticated conversation API，必须经过 Agent Planner。
- `gateway_owned_service_paths`：models/count_tokens/tools/call/assistants/threads 等 Gateway-owned public endpoint，必须产生 Gateway runtime event。
- `control_plane_paths_excluded`：`/ui`、`/healthz`、`/admin/agent-runtime-audit.json` 等 admin/observability/control-plane，不创建用户 planner session。
- `security_layer_excluded`：auth failures、admin auth failures、unsupported 404 在信任 tenant/workspace/session 之前终止，不纳入用户对话 Planner scope。

代码/测试：
- `src/gateway_http_handler.py` 新增 `_agent_runtime_scope_contract()` 并挂到 `/admin/agent-runtime-audit.json` 返回体。
- `tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions` 固定 contract。

Live 验证：
```text
GET /admin/agent-runtime-audit.json?limit=40
  strict_conversation_scope=supported_authenticated_public_api_paths
  conversation_count=6
  gateway_owned_count=15
  has_auth_exclusion=true
  has_404_exclusion=true
  has_admin_audit_exclusion=true
```

## 2026-06-27 Agent Planner final synthesis 兜底加固

**状态**: 已修复、已重启 live `127.0.0.1:8885`、已用原始失败请求重放验证。

用户反馈的失败链路：Claude/Codex client 在 `/v1/messages` 发送 `分析这套项目`，请求里有 62 个工具，Planner 实际已进入 `project_analysis` 并收集了 `/Users/sanbo/Desktop/ti` 的工具证据，但最后一步交给 chat-only 上游做文字综合时，上游返回：`Hello, I can't answer this question for now. Let's talk about something else.`

根因结论：不是“没有进入 Agent Planner”，而是 **final synthesis 边界太相信 chat-only 上游**。远端服务的 Agent Planner 已拥有 workflow/state/evidence，上游只允许做文字综合；如果上游拒答、串旧 session/workspace、或输出“Let me first see...”这类无工具调用占位话术，Gateway 不能原样透传。

修复：
- `src/gateway_agent_planner.py`
  - `apply_synthesis_refusal_fallback()` 扩展为 final synthesis 质量闸。
  - 拦截三类不合格上游综合：
    1. 拒答/换话题：`I can't answer...`、`Let's talk about something else`、中文无法回答等。
    2. 跨 session/workspace 漂移：`上一个 session`、`正确的路径`、或答案提到不属于当前 planner workspace/evidence 的绝对路径。
    3. 无工具调用占位：`Let me first see/check/inspect...`、`我先看看/先检查一下`。
  - 命中后改用 deterministic planner evidence synthesis，并标记：
    - `gateway_agent_planner.synthesis_refusal_fallback`
    - `gateway_agent_planner.synthesis_scope_fallback`
    - `gateway_agent_planner.synthesis_nonanswer_fallback`
- `tests/test_gateway.py`
  - 新增/补强 final synthesis 回归：拒答、跨 session 路径漂移、无工具调用占位都不能泄漏给客户端。

Live 重放旧失败请求 `request_logs.id=6151` 后结果：
```text
stop=end_turn
workflow=project_analysis
step=synthesis
synthesis_refusal_fallback=False
synthesis_scope_fallback=True
synthesis_nonanswer_fallback=True
has_old_refusal=False
has_wrong_path=False
has_let_me=False
```
返回内容改为基于 planner evidence 的兜底摘要，包含：
- `Gateway Agent Planner: no known test runner found`
- `/Users/sanbo/Desktop/ti/pyproject.toml` 不存在
- `/Users/sanbo/Desktop/ti/go.mod` 不存在
- `/Users/sanbo/Desktop/ti/package.json` 不存在

Live `jo -> 分析这套项目` 链路：
```text
jo:
  stop=end_turn
  workflow=chat_only_synthesis
  intent=plain_chat

分析这套项目:
  stop=tool_use
  tool=Bash
  workflow=project_analysis
  step=project_structure
  intent=project_analysis
```

验证：
```bash
python3 -m pytest -q \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_uses_history_only_for_followup_not_plain_thanks \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_plain_thanks_after_project_history_does_not_dispatch_project_tool \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_uses_history_for_explicit_project_followup \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_history_validation_does_not_pollute_plain_followup \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_chat_only_refusal_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_cross_session_path_drift_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_final_synthesis_nonanswer_after_evidence \
  tests/test_agent_planner_client_context.py
# 8 passed

python3 tests/integration/agent_planner_protocol_strict_smoke.py
# ok=true; covered_session_count=12; missing_session_count=0; strict_runtime_scope=true

python3 tests/integration/agent_planner_public_surface_smoke.py
# ok=true; advertised_count=21; strict missing_session_count=0

python3 tests/integration/agent_planner_project_analysis_smoke.py
# ok=true; final_text=PROJECT-ANALYSIS-FINAL-OK

GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest -q
# 988 passed, 2 skipped, 21 warnings
```

当前判断：这条“进入 Planner 但最终上游乱答/拒答”的链路已经被 Gateway 兜住。若客户端再次看到旧拒答，优先按 request log id / tenant / workspace / session 查是否连到当前重启后的 8885 进程。

### 2026-06-27 synthesis guard integration smoke 补齐

为避免 final synthesis 保护只停留在单测和一次 live 重放，本轮新增可重复 integration smoke：

- `tests/integration/agent_planner_synthesis_guard_smoke.py`

覆盖三类 chat-only 上游不合格最终综合：

1. `refusal`: `Hello, I can't answer this question...`
2. `scope`: `上一个 session` / `/Users/sanbo/Desktop/old-project` 跨 workspace 漂移
3. `nonanswer`: `Let me first see what's actually in that directory.`

验证结果：
```bash
python3 tests/integration/agent_planner_synthesis_guard_smoke.py
# ok=true; cases=[refusal, scope, nonanswer]
# 每个 case 都替换为 planner evidence fallback，并设置对应 metadata flag

python3 -m pytest -q \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_chat_only_refusal_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_cross_session_path_drift_after_evidence \
  tests/test_gateway.py::WeakUpstreamToolRoundSurfacingTests::test_agent_planner_does_not_leak_final_synthesis_nonanswer_after_evidence
# 3 passed
```

这补齐了“进入 Planner 但最终上游拒答/串旧 workspace/假装继续看目录”的可重复集成门禁。

### 2026-06-27 Agent Planner acceptance gate 统一验收入口

为避免“每个能力分别跑过，但没有统一入口证明 Agent Planner runtime 整体可用”，本轮新增：

- `scripts/agent_planner_acceptance.sh`

默认 smoke 模式串行执行：

1. `tests/integration/agent_planner_synthesis_guard_smoke.py`
2. `tests/integration/agent_planner_project_analysis_smoke.py`
3. `tests/integration/agent_planner_multiround_smoke.py`
4. `tests/integration/agent_planner_protocol_strict_smoke.py`
5. `tests/integration/agent_planner_public_surface_smoke.py`
6. `tests/integration/agent_planner_remote_pressure_smoke.py`
7. `tests/integration/agent_planner_long_context_pressure_smoke.py`
8. 重点 planner/client-context pytest 回归 8 条

可选 `--full` 会在 smoke 后追加全量 pytest：

```bash
./scripts/agent_planner_acceptance.sh
./scripts/agent_planner_acceptance.sh --full
```

本轮执行结果：

```text
./scripts/agent_planner_acceptance.sh
# Agent Planner acceptance gate: PASS
# protocol strict: covered_session_count=12, missing_session_count=0
# public surface: advertised_count=21, strict missing_session_count=0
# remote pressure: users=6, planner_sessions_checked=6, memory_rollups_checked=6
# long context: users=4, cross_tenant_leak_checked=true, compacted=true
# focused pytest: 8 passed
```

说明：某些 integration smoke 使用独立临时 runtime/store，证明结果不会全部留在当前 live `127.0.0.1:8885` 的全局 admin audit 中。因此完整验收以后以该 acceptance gate 为入口；单个 live admin audit 仍用于当前进程/当前 tenant/workspace/session 的 scoped runtime proof。

### 2026-06-27 acceptance gate 扩展公开功能兼容测试

本轮继续补齐“每一个功能都必须支持”的验收覆盖：`scripts/agent_planner_acceptance.sh` 已加入公开功能兼容面的专门测试，而不只依赖 public surface smoke 的最小 HTTP 调用。

新增纳入 acceptance 的测试：

- `tests/test_gateway_assistants.py`
  - assistants/threads 是 Gateway-owned endpoint，不转发给 chat-only upstream。
  - workspace metadata 优先级正确。
- `tests/test_gateway_proxy_errors.py`
  - upstream HTTP 错误保留状态与 detail。
- `tests/test_gateway.py::NativeGatewayTests::test_models_and_count_tokens_endpoints_for_claude_code_compatibility`
  - models/count_tokens 兼容 Claude Code 常用路径。
- `tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions`
  - audit scope contract 固定 conversation / gateway-owned / control-plane / security-layer 边界。

验证：

```bash
python3 -m pytest -q \
  tests/test_gateway_assistants.py \
  tests/test_gateway_proxy_errors.py \
  tests/test_agent_planner_client_context.py \
  tests/test_gateway.py::NativeGatewayTests::test_models_and_count_tokens_endpoints_for_claude_code_compatibility \
  tests/test_gateway.py::NativeGatewayTests::test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions
# 9 passed

./scripts/agent_planner_acceptance.sh
# Agent Planner acceptance gate: PASS
# focused pytest inside gate: 16 passed
```

当前 acceptance gate 覆盖面进一步扩展到：conversation strict planner、all advertised public paths、assistants/threads、models/count_tokens、direct tools/functions、proxy error shape、history pollution、final synthesis guard、multi-user pressure、long-context streaming compaction。

### 2026-06-27 Agent Planner completion matrix 固化

新增完成度验收矩阵：

- `docs/agent-runtime-completion-matrix.md`

该文档把用户目标拆成 25 个 requirement，并明确每个 requirement 需要哪些证据才能算完成：strict conversation envelope、21 个 public paths、Gateway-owned endpoint、项目分析 workflow、多轮工具结果、final synthesis guard、历史 follow-up vs 污染、多用户隔离、长上下文、streaming parity、admin observability、scope contract、acceptance gate、full regression。

关键原则：

- 单个 global admin audit 不能证明全局完成；它只是 operator overview。
- scoped tenant/workspace/session audit 用于证明某个 live conversation。
- `./scripts/agent_planner_acceptance.sh` 是 smoke-level 全局验收入口。
- 最终要调用 `update_goal complete` 前，必须在同一证据窗口跑 `./scripts/agent_planner_acceptance.sh --full`，并同时通过 live health、scoped audit、diff check、secret grep。

这一步的意义：防止后续把“某个功能刚测过”误判为“所有功能完成”。

## 2026-06-27 04:46 Agent Planner live-stability fix

- 修复 final synthesis guard 的响应文本抽取：不再按请求 path 单一路径读取，统一兼容 Anthropic `content`、OpenAI Responses `output/output_text`、OpenAI Chat `choices[].message.content`，避免正常最终回答（如 `result is 3` / `saw x.py`）被误判为空并替换成 deterministic fallback。
- 修复验收脚本 `--full`：避免调用用户 PATH 中的 `/Users/sanbo/.local/bin/env` 导致 `env VAR=0 python3 ...` 静默秒退，改为 subshell `export GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0; python3 -m pytest -ra tests`，确保 full pytest 输出真实汇总。
- 调整默认测试配置：`_default_config()` 的 `agent_planner_strict_every_turn` 默认从 env 读取、缺省为 false；远端/本地服务仍由 `gateway.config.json` / `.gateway_service.json` 显式 true 控制，避免兼容性单测被 strict remote mode 污染。
- 补齐 no-declared-tools 项目分析 fallback 的可观测性：`分析这套项目` 即使请求没有显式 tools，也会返回 downstream `LS/Glob/Glob` tool_use，并在响应 `gateway_context.agent_planner` 中标记 `workflow=project_analysis`、`step=project_structure`；runtime event 标记 `owner=downstream_client` / `dispatch=downstream_client`，scoped audit 能正确计入 strict coverage。
- 已重启本地 8885 服务并完成真实 HTTP 验证：`/v1/messages` + `分析这套项目` 返回 `stop_reason=tool_use`、tools=`LS, Glob, Glob`、`strategy=gateway_downstream_tool_request`、`planner_workflow=project_analysis`、`planner_step=project_structure`。
- Scoped live audit 通过：`strict_every_turn_planner_envelope=proven/current_scope`，`covered_session_count=1`，`dispatch_session_count=1`，`missing_session_count=0`。
- 验证：`./scripts/agent_planner_acceptance.sh --full` 最终通过，full pytest 汇总为 `988 passed, 2 skipped, 21 warnings in 51.97s`。


## 2026-06-27 04:50 Agent Planner completion audit

- Completion matrix `docs/agent-runtime-completion-matrix.md` updated from "strong smoke" to verified-current for R1-R25.
- Final evidence window includes: full acceptance `988 passed, 2 skipped, 21 warnings`; live health on 8885; live `/v1/messages` project-analysis dispatch to `LS/Glob/Glob`; scoped audit `strict_every_turn_planner_envelope=proven/current_scope`; clean `git diff --check`; secret grep with only fake/test keys.
- Important caveat retained in docs: broad tenant/workspace audits may include historical failed sessions; authoritative live strict proof is tenant + workspace + session scoped. Future code/config/upstream changes must rerun the full gate and scoped live audit before re-claiming completion.
