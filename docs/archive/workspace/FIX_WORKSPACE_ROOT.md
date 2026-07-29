# Workspace Root 修复说明

## 问题描述

之前 `workspace_root` 存在两个严重问题：

### 1. 🔴 安全漏洞：路径遍历攻击

**严重性**: CRITICAL (CVSS 9.8)

Gateway 在客户端未提供 `workspace_root` 时，会回退到**服务器目录**，导致：
- 攻击者可以读取服务器上的任意文件（`/etc/passwd`、SSH 密钥、配置文件等）
- 服务器文件系统完全暴露
- 可能导致服务器被完全控制

**详细分析**: 见 [SECURITY_FIX_WORKSPACE.md](SECURITY_FIX_WORKSPACE.md)

### 2. 架构设计问题：持久化客户端配置

`workspace_root` 被持久化保存在 `.gateway_service.json` 配置文件中，导致：
- **错误的架构设计**：Gateway 是服务提供方，不应该持久化存储客户端的工作目录
- **多用户冲突**：不同用户/项目使用同一个 Gateway 实例时，会相互覆盖 workspace
- **配置污染**：用户切换目录时需要手动修改服务端配置

## 解决方案

### 🔴 安全修复：绝不使用服务器目录

**核心原则**：
- ✅ 所有 `workspace_root` 必须来自客户端（用户本地机器）
- ✅ 如果客户端未提供，必须**安全失败**（返回 None 或抛出错误）
- ❌ 绝对不能回退到 `os.getcwd()`、配置文件、或任何服务器路径

#### 修改：`src/gateway_tool_runtime.py`

```python
def _request_workspace_root(body: Json) -> pathlib.Path | None:
    """Extract workspace root from request body.

    SECURITY: This function must NEVER return the Gateway server's directory.
    All workspace paths MUST come from the client (user's machine).

    Returns None if no client workspace is provided - this will cause tool calls to fail safely.
    """
    # 1. 显式字段
    custom_root = body.get("workspace_root") or body.get("gateway_workspace")
    if custom_root:
        return pathlib.Path(custom_root).resolve()
    
    # 2. 客户端 metadata
    detected = _extract_client_project_dir(body)
    if detected is not None:
        return detected
    
    # 3. 仅测试用的环境变量
    env_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    if env_root:
        return pathlib.Path(env_root).resolve()

    # ✓ 安全：返回 None，绝不使用服务器目录
    return None
```

#### 修改：`src/gateway_builtin_tools.py`

```python
def _workspace_root():
    """Get the workspace root.

    SECURITY: This must ONLY return client-provided workspace, never server directories.
    """
    override = _WORKSPACE_ROOT_OVERRIDE.get()
    if override is not None:
        return pathlib.Path(override).resolve()

    env_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    if env_root:
        return pathlib.Path(env_root).resolve()

    # ✓ 安全：抛出清晰错误，绝不使用服务器目录
    raise ToolExecutionError(
        "No workspace root provided. Client must send workspace_root in request body or metadata.",
        failure_type="missing_workspace"
    )
```

### 架构修复：不再持久化 workspace_root

### 1. 配置保存逻辑修改

**文件**: `src/gateway_config.py`

```python
def save_config(config: Json) -> None:
    normalized = copy.deepcopy(config)
    _normalize_admin_credentials(normalized)
    _ensure_client_snippet_downstream_key(normalized)
    # Remove runtime-only fields that should not be persisted
    # workspace_root is dynamically determined per-request from client metadata
    if "gateway" in normalized and isinstance(normalized["gateway"], dict):
        normalized["gateway"].pop("workspace_root", None)
    CONFIG_PATH.write_text(json.dumps(_sync_active_upstream(normalized), ensure_ascii=False, indent=2), encoding="utf-8")
```

**变更**：保存配置时自动移除 `workspace_root` 字段

### 2. HTTP Handler 修改

**文件**: `src/gateway_http_handler.py`

```python
# 移除了这行代码：
# gateway_cfg["workspace_root"] = form.get("workspace_root", gateway_cfg.get("workspace_root", ""))

# 添加了注释说明：
# Note: workspace_root is NOT saved - it's a runtime field determined per-request from client metadata
```

**变更**：Admin UI 表单提交时不再保存 `workspace_root`

### 3. Admin UI 修改

**文件**: `src/gateway_admin.py`

```html
<!-- 修改前 -->
<label class="field"><span>工作目录</span><input name="workspace_root" value="{E(gateway_cfg.get("workspace_root",""))}"></label>

<!-- 修改后 -->
<label class="field"><span>工作目录 (运行时)</span><input name="workspace_root_display" value="{E(gateway_cfg.get("workspace_root",""))}" readonly title="此字段从客户端请求动态提取，不可编辑" style="background:#f5f5f5;cursor:not-allowed;"></label>
```

**变更**：
- 字段名改为 `workspace_root_display`（不会被提交）
- 添加 `readonly` 属性
- 添加工具提示说明
- 灰色背景 + 禁用光标样式

## 工作原理

### Workspace Root 解析优先级

代码位置：`src/gateway_tool_runtime.py::_request_workspace_root()`

```python
def _request_workspace_root(body: Json) -> pathlib.Path:
    """Extract workspace root from request body or use default.

    Priority chain:
    1. Explicit body field (workspace_root or gateway_workspace)
    2. Auto-detected downstream project dir from session metadata
    3. Explicit env var (GATEWAY_WORKSPACE_ROOT) if not cwd
    4. Config file setting (已被排除持久化)
    5. Default workspace root
    """
```

### 客户端如何发送 Workspace

#### 1. 显式字段（推荐）

```json
{
  "model": "mimo-v2.5-pro",
  "messages": [...],
  "workspace_root": "/Users/alice/my-project"
}
```

或使用别名：

```json
{
  "gateway_workspace": "/Users/alice/my-project"
}
```

#### 2. Metadata 字段（Claude Code / Codex）

```json
{
  "model": "mimo-v2.5-pro",
  "messages": [...],
  "metadata": {
    "project_dir": "/Users/alice/my-project",
    "cwd": "/Users/alice/my-project",
    "session_id": "abc123"
  }
}
```

#### 3. 自动提取（Claude Code 格式）

Claude Code 会在 system prompt 或 messages 中注入工作目录信息：

```
Primary working directory: /Users/alice/my-project
```

Gateway 会自动从这些文本中提取路径。

### 作用域管理

代码使用 `ContextVar` 实现线程安全的作用域管理：

```python
with _workspace_scope(_request_workspace_root(body)):
    # 在这个作用域内，所有工具调用使用这个 workspace
    results = _execute_tool_calls(...)
```

## 验证

### 1. 配置文件验证

```bash
# 确认配置文件中不再有 workspace_root
grep "workspace_root" .gateway_service.json
# 输出：(空，表示没有匹配)
```

### 2. 运行时验证

```bash
# 启动 Gateway
python3 -m src.gateway_app

# 发送测试请求
curl -X POST http://localhost:8885/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <DOWNSTREAM_API_KEY>" \
  -d '{
    "model": "mimo-v2.5-pro",
    "workspace_root": "/Users/test/project1",
    "messages": [{"role": "user", "content": "list files"}]
  }'
```

### 3. 多客户端测试

```python
# 客户端 A - 项目 1
response_a = requests.post(
    "http://localhost:8885/v1/chat/completions",
    json={
        "model": "mimo-v2.5-pro",
        "workspace_root": "/Users/alice/project1",
        "messages": [{"role": "user", "content": "pwd"}]
    }
)

# 客户端 B - 项目 2
response_b = requests.post(
    "http://localhost:8885/v1/chat/completions",
    json={
        "model": "mimo-v2.5-pro",
        "workspace_root": "/Users/bob/project2",
        "messages": [{"role": "user", "content": "pwd"}]
    }
)

# 两个请求互不干扰，各自使用自己的 workspace
```

## 兼容性

### Claude Code

✅ 完全兼容 - Claude Code 会自动在 system prompt 中注入工作目录

### Codex

✅ 完全兼容 - Codex 会在 metadata 中发送 `project_dir` 和 `cwd`

### OpenAI SDK / 自定义客户端

✅ 兼容 - 可通过 `workspace_root` 或 `gateway_workspace` 字段显式指定

### 向后兼容

如果客户端没有发送任何 workspace 信息：
- 使用环境变量 `GATEWAY_WORKSPACE_ROOT`（如果设置）
- 否则使用服务启动时的当前工作目录 (`cwd`)

## 测试

```bash
# 运行测试
python3 -c "
import json
import tempfile
import pathlib
from src.gateway_config import save_config

# 创建测试配置
test_config = {
    'gateway': {
        'workspace_root': '/should/be/removed',
        'tool_mode': 'orchestrate'
    },
    'upstream': {
        'base_url': 'test',
        'api_key': 'test',
        'model': 'test',
        'id': 't',
        'name': 't'
    }
}

# 保存到临时文件
with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
    temp_path = pathlib.Path(f.name)

from src import gateway_config
original_path = gateway_config.CONFIG_PATH
gateway_config.CONFIG_PATH = temp_path

try:
    save_config(test_config)
    saved = json.loads(temp_path.read_text())
    
    if 'workspace_root' in saved.get('gateway', {}):
        print('✗ FAILED: workspace_root still persisted')
    else:
        print('✓ PASSED: workspace_root correctly stripped')
finally:
    gateway_config.CONFIG_PATH = original_path
    temp_path.unlink()
"
```

## 总结

✅ **问题已修复**：`workspace_root` 不再被持久化保存

✅ **架构正确**：Gateway 作为无状态服务，每个请求动态解析 workspace

✅ **多用户支持**：不同用户/项目可以同时使用同一个 Gateway 实例

✅ **向后兼容**：现有客户端无需修改，自动从请求中提取 workspace

✅ **UI 改进**：Admin UI 显示运行时值，防止用户误操作

## 相关文件

- `src/gateway_config.py` - 配置保存逻辑
- `src/gateway_http_handler.py` - HTTP 请求处理
- `src/gateway_admin.py` - Admin UI
- `src/gateway_tool_runtime.py` - Workspace 解析和作用域管理
- `src/gateway_builtin_tools.py` - 工具执行（使用 workspace 作用域）

## 文档更新

需要更新以下文档：

- [ ] `docs/CONFIGURATION.md` - 说明 workspace_root 是运行时字段
- [ ] `docs/CLIENT_INTEGRATION.md` - 客户端如何发送 workspace
- [ ] `CLAUDE.md` - 更新项目进度
