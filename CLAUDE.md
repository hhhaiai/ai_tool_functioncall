# Gateway 项目进度跟踪

> 最后更新: 2026-06-19

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

1. 高性能优化 - 替换 ThreadingHTTPServer 为 asyncio/aiohttp (百亿 token/小时)
2. gateway_stats.py 查询优化 (推送聚合到 SQL)
3. Guardrails (输入/输出验证)
4. OpenTelemetry 集成
