# Workspace Root 最终设计

## 核心原则

### 🔴 绝对红线

**Gateway 绝对不能使用服务器目录作为工作空间**

- ❌ 不能使用 `os.getcwd()`
- ❌ 不能使用服务器的任何路径
- ❌ 不能提供 Gateway 服务本身的目录/空间/执行 shell 等操作给用户

### ✅ 正确设计

**最大程度保证用户的稳定请求和稳定服务**

- ✅ 用户即使不提供 workspace，也能正常对话
- ✅ 每个用户/会话自动获得**隔离的匿名空间**
- ✅ 基于 `session_id` 生成稳定的匿名目录
- ✅ 绝对不会失败，保证服务连续性

---

## 工作机制

### Workspace 解析优先级

```python
def _request_workspace_root(body: Json) -> pathlib.Path:
    """
    Priority chain:
    1. 客户端显式提供: body.workspace_root / body.gateway_workspace
    2. 客户端 metadata: metadata.project_dir, metadata.cwd
    3. 自动提取: 从 system/messages 中提取（Claude Code 格式）
    4. 测试环境变量: GATEWAY_WORKSPACE_ROOT
    5. 匿名隔离空间: ~/.gateway_runtime/anonymous_spaces/{session_id}
    
    ✅ 永远返回有效路径，绝不失败
    ❌ 绝不返回 Gateway 服务器目录
    """
```

### 匿名空间机制

```python
def _create_anonymous_workspace(body: Json) -> pathlib.Path:
    """
    为每个会话创建隔离的临时目录
    
    Session ID 来源（按优先级）：
    1. metadata.session_id 或 metadata.conversation_id
    2. hash(model + first_user_message)  # 同一对话稳定
    3. random UUID  # 完全匿名
    
    返回: ~/.gateway_runtime/anonymous_spaces/{session_id}/
    """
```

**特点**：
- ✅ 每个会话隔离（不会互相干扰）
- ✅ 同一 session_id 获得相同空间（对话持续性）
- ✅ 完全隔离于 Gateway 服务器目录
- ✅ 用户可以正常使用文件工具（Read/Write/Glob/Grep）

---

## 示例场景

### 场景 1：Claude Code 用户（有 workspace）

```json
{
  "model": "mimo-v2.5-pro",
  "metadata": {
    "project_dir": "/Users/alice/my-project",
    "session_id": "9ed53126-276e-4591-9560-b79d879f7d95"
  },
  "messages": [...]
}
```

**结果**：使用 `/Users/alice/my-project` 作为 workspace ✅

---

### 场景 2：匿名用户（无 workspace）

```json
{
  "model": "mimo-v2.5-pro",
  "messages": [{"role": "user", "content": "Hello"}]
}
```

**结果**：自动创建匿名空间
```
~/.gateway_runtime/anonymous_spaces/70441ab89ced962c/
```

用户可以正常使用：
- `Read` 工具读取文件
- `Write` 工具创建文件
- `Bash` 工具运行命令（在匿名空间内）

**完全隔离**，不会影响 Gateway 服务器 ✅

---

### 场景 3：持续对话（有 session_id）

**第一次请求**：
```json
{
  "metadata": {"session_id": "session-abc"},
  "messages": [{"role": "user", "content": "Create a file test.txt"}]
}
```

**匿名空间**：`~/.gateway_runtime/anonymous_spaces/session-abc/`  
**操作**：创建 `test.txt`

**第二次请求**（同一 session）：
```json
{
  "metadata": {"session_id": "session-abc"},
  "messages": [{"role": "user", "content": "Read test.txt"}]
}
```

**匿名空间**：`~/.gateway_runtime/anonymous_spaces/session-abc/` （相同！）  
**操作**：读取之前创建的 `test.txt` ✅

---

## 安全保证

### ✅ 保护 Gateway 服务器

```python
# Gateway 服务器目录
server_cwd = "/opt/gateway"

# 用户请求（无 workspace）
body = {}

# 返回匿名空间，绝不返回服务器目录
result = _request_workspace_root(body)
assert str(result) != server_cwd  # ✓ 安全
assert ".gateway_runtime/anonymous_spaces" in str(result)  # ✓ 隔离
```

### ✅ 会话隔离

```python
# 会话 A
body_a = {"metadata": {"session_id": "session-A"}}
workspace_a = _request_workspace_root(body_a)
# -> ~/.gateway_runtime/anonymous_spaces/session-A/

# 会话 B
body_b = {"metadata": {"session_id": "session-B"}}
workspace_b = _request_workspace_root(body_b)
# -> ~/.gateway_runtime/anonymous_spaces/session-B/

assert workspace_a != workspace_b  # ✓ 隔离
```

### ✅ 服务稳定性

```python
# 即使客户端完全不提供任何信息
body = {}

# 也能正常工作，不会失败
result = _request_workspace_root(body)
assert result is not None  # ✓ 永远返回有效路径
assert result.exists()  # ✓ 自动创建目录
```

---

## 架构对比

### ❌ 修复前（危险）

```
用户请求（无 workspace）
    ↓
Gateway 使用 os.getcwd()
    ↓
暴露服务器目录：/opt/gateway
    ↓
用户可读取：
  - /opt/gateway/.env
  - /opt/gateway/config.json
  - /etc/passwd
  - ~/.ssh/id_rsa
```

### ✅ 修复后（安全）

```
用户请求（无 workspace）
    ↓
Gateway 创建匿名空间
    ↓
隔离目录：~/.gateway_runtime/anonymous_spaces/{session_id}/
    ↓
用户只能访问自己的匿名空间
    ↓
Gateway 服务器目录完全隔离
```

---

## Gateway 服务定位

### Gateway = 纯服务提供方

```
┌──────────────────┐              ┌────────────────────┐
│   用户本地机器    │              │  Gateway 服务器    │
│                  │              │                    │
│  客户端代码      │──请求─────→  │  协议转换          │
│  (Claude Code)   │              │  Tool calls支持    │
│                  │              │  无限上下文        │
│  workspace:      │←───响应──── │  智能缓存          │
│  /Users/alice/   │              │                    │
│                  │              │  ❌ 不操作本机     │
│                  │              │  ❌ 不访问本地文件 │
└──────────────────┘              └────────────────────┘
         ↓                                 ↓
  用户工作目录                      匿名隔离空间
  或                              ~/.gateway_runtime/
  匿名空间                         anonymous_spaces/
```

**服务内容**：
- ✅ 协议转换（OpenAI ↔ Anthropic）
- ✅ Tool calls 完整支持
- ✅ 无限上下文 + 智能缓存
- ✅ 并发管理 + 负载均衡
- ✅ Skills/MCP/Agent（提升能力）

**绝对不提供**：
- ❌ Gateway 服务器目录访问
- ❌ 服务器文件系统操作
- ❌ 本地预演/测试（未来会有安全沙箱）

---

## 测试验证

### ✅ 全部测试通过（6/6）

```bash
pytest tests/test_gateway.py -k 'workspace and not streaming_passthrough'
# 6 passed

# 通过的测试：
✓ test_delete_path_refuses_workspace_root_even_recursive
✓ test_direct_tool_call_can_scope_workspace_per_request
✓ test_gateway_internal_workspace_fields_are_not_forwarded_upstream
✓ test_read_glob_grep_tools_respect_workspace_root
✓ test_workspace_scope_is_thread_local_for_parallel_downstream_projects
✓ test_workspace_tools_reject_paths_outside_workspace_root
```

### ✅ 匿名空间测试通过（5/5）

```python
✓ Anonymous workspace created successfully
✓ Session workspace is stable (same session_id -> same path)
✓ Sessions are isolated (different session_id -> different path)
✓ Client workspace has priority
✓ Server directory is protected
```

---

## 文档更新

- **FINAL_DESIGN_WORKSPACE.md** - 本文档（最终设计）
- **SECURITY_FIX_WORKSPACE.md** - 安全漏洞分析
- **FIX_WORKSPACE_ROOT.md** - 技术修复说明
- **SUMMARY_WORKSPACE_FIX.md** - 修复总结
- **CLAUDE.md** - 项目进度

---

## 总结

### 核心改进

1. **🔴 安全修复**：绝不使用服务器目录
2. **✅ 稳定服务**：匿名空间保证连续性
3. **✅ 会话隔离**：每个 session 独立空间
4. **✅ 向后兼容**：客户端无需修改

### 关键特性

- ✅ **用户永远不会失败** - 即使不提供 workspace
- ✅ **服务器永远安全** - 目录完全隔离
- ✅ **会话稳定持续** - 同一 session_id 复用空间
- ✅ **客户端优先** - 提供 workspace 时使用客户端的

**Gateway 的使命**：提供稳定、安全、高质量的 AI 服务
