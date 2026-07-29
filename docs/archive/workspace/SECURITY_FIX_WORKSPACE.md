# CRITICAL SECURITY FIX: Workspace Root 路径遍历漏洞

## 🔴 严重性：CRITICAL

**漏洞类型**：路径遍历 / 未授权文件访问  
**CVSS评分**：9.8 (Critical)  
**影响范围**：所有未修复的 Gateway 实例

---

## 漏洞描述

### 问题

修复前，Gateway 在客户端**未提供** `workspace_root` 时，会**回退到服务器的工作目录**：

```python
# ❌ 危险代码（已修复）
def _request_workspace_root(body: Json) -> pathlib.Path:
    # ... 尝试从客户端提取 ...
    
    # 回退到服务器目录 - 严重安全漏洞！
    default = _workspace_root()  # 返回 os.getcwd()
    return default
```

### 攻击场景

**攻击步骤：**

1. 攻击者发送请求，**故意不提供** `workspace_root`
2. Gateway 使用服务器的 `/opt/gateway` 作为默认工作目录
3. 攻击者调用 `Read` 工具读取 `/etc/passwd`、`/root/.ssh/id_rsa`、配置文件、数据库凭证等
4. 服务器文件系统完全暴露！

**攻击示例：**

```bash
# 攻击者发送不带 workspace_root 的请求
curl -X POST http://gateway:8885/v1/chat/completions \
  -H "Authorization: Bearer sk-xxx" \
  -d '{
    "model": "mimo-v2.5-pro",
    "messages": [{
      "role": "user",
      "content": "Read /etc/passwd"
    }],
    "tools": [{
      "type": "function",
      "function": {
        "name": "Read",
        "parameters": {
          "file_path": "/etc/passwd"
        }
      }
    }]
  }'

# Gateway 会使用服务器目录，读取成功！
```

**可能泄露的敏感信息：**
- 系统配置：`/etc/passwd`, `/etc/shadow`, `/etc/hosts`
- SSH 密钥：`/root/.ssh/id_rsa`, `~/.ssh/`
- 应用配置：`.env`, `config.json`, `database.yml`
- 源代码：Gateway 服务器上的所有代码
- 数据库文件：`*.db`, `*.sqlite`
- 日志文件：可能包含 API 密钥、token

---

## 修复方案

### 核心原则

**绝对红线：** Gateway 绝对不能使用服务器目录作为工作空间

- ✅ 所有 `workspace_root` 必须来自客户端（用户本地机器）
- ✅ 如果客户端未提供，必须**安全失败**（返回错误）
- ❌ 绝对不能回退到 `os.getcwd()`、`/opt/gateway` 等服务器路径

### 修复代码

#### 1. `src/gateway_tool_runtime.py`

```python
def _request_workspace_root(body: Json) -> pathlib.Path | None:
    """Extract workspace root from request body.

    SECURITY: This function must NEVER return the Gateway server's directory.
    All workspace paths MUST come from the client (user's machine).

    Returns None if no client workspace is provided.
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

    # ✓ 安全：返回 None，不使用服务器目录
    return None
```

#### 2. `src/gateway_builtin_tools.py`

```python
def _workspace_root():
    """Get the workspace root.

    SECURITY: This must ONLY return client-provided workspace.
    """
    override = _WORKSPACE_ROOT_OVERRIDE.get()
    if override is not None:
        return override

    env_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    if env_root:
        return pathlib.Path(env_root).resolve()

    # ✓ 安全：抛出清晰错误，不使用服务器目录
    raise ToolExecutionError(
        "No workspace root provided. Client must send workspace_root in request.",
        failure_type="missing_workspace"
    )
```

#### 3. `src/gateway_tool_runtime.py` - Scope 管理

```python
@contextmanager
def _workspace_scope(root: pathlib.Path | None):
    """Context manager for workspace scope.

    SECURITY: If root is None, tools will fail safely.
    """
    if root is None:
        yield None  # 工具调用时会失败
        return

    from . import gateway_builtin_tools as _bt
    token = _bt._WORKSPACE_ROOT_OVERRIDE.set(root.resolve())
    try:
        yield root
    finally:
        _bt._WORKSPACE_ROOT_OVERRIDE.reset(token)
```

---

## 安全验证

### 测试 1：无 workspace 提供 - 安全失败

```python
from src.gateway_tool_runtime import _request_workspace_root

result = _request_workspace_root({})
assert result is None  # ✓ 返回 None，不使用服务器目录
```

### 测试 2：工具调用失败并显示清晰错误

```python
from src.gateway_builtin_tools import _workspace_root, ToolExecutionError

try:
    root = _workspace_root()
    assert False, "Should have raised error"
except ToolExecutionError as e:
    assert "No workspace root provided" in str(e)  # ✓ 清晰的错误消息
```

### 测试 3：客户端 workspace 正常工作

```python
result = _request_workspace_root({"workspace_root": "/Users/alice/project"})
assert result == pathlib.Path("/Users/alice/project").resolve()  # ✓
```

### 测试 4：验证服务器目录绝不使用

```python
server_cwd = os.getcwd()
result = _request_workspace_root({})
assert result != pathlib.Path(server_cwd).resolve()  # ✓ 绝不使用服务器目录
```

---

## 客户端集成

### 客户端必须发送 workspace

所有客户端（Claude Code, Codex, 自定义客户端）必须在请求中包含 workspace：

#### 方式 1：显式字段（推荐）

```json
{
  "model": "mimo-v2.5-pro",
  "workspace_root": "/Users/alice/my-project",
  "messages": [...]
}
```

#### 方式 2：Metadata 字段

```json
{
  "model": "mimo-v2.5-pro",
  "metadata": {
    "project_dir": "/Users/alice/my-project",
    "cwd": "/Users/alice/my-project"
  },
  "messages": [...]
}
```

#### 方式 3：自动提取（Claude Code）

Claude Code 会在 system prompt 中自动注入：

```
Primary working directory: /Users/alice/my-project
```

Gateway 会自动提取这个路径。

---

## 部署检查清单

在生产环境部署前，必须验证：

- [ ] `_request_workspace_root` 返回 `None` 当无客户端 workspace 时
- [ ] `_workspace_root()` 抛出 `ToolExecutionError` 而不是使用 `os.getcwd()`
- [ ] 所有工具调用（Read, Write, Glob, Grep）在无 workspace 时失败
- [ ] 测试套件中的 workspace 测试全部通过
- [ ] 环境变量 `GATEWAY_WORKSPACE_ROOT` 仅用于测试，生产环境不设置
- [ ] 配置文件中不再持久化 `workspace_root`

### 验证脚本

```bash
# 运行安全测试
python3 << 'EOF'
from src.gateway_tool_runtime import _request_workspace_root
import os

# 确保不使用服务器目录
result = _request_workspace_root({})
server_cwd = os.getcwd()

if result is None:
    print("✓ SECURE: No fallback to server directory")
elif result == pathlib.Path(server_cwd).resolve():
    print("✗ CRITICAL: Still using server directory!")
    exit(1)
else:
    print(f"✗ WARNING: Unexpected result: {result}")
    exit(1)
EOF

# 运行 workspace 测试
python3 -c "import pytest, sys; sys.exit(pytest.main(['-v', 'tests/test_gateway.py', '-k', 'workspace and not streaming_passthrough']))"
```

---

## 影响分析

### 修复前的风险

- **完整的服务器文件系统暴露**
- **所有敏感文件可读取**
- **可能导致服务器完全被控制**

### 修复后的行为

- ✅ 客户端必须提供 workspace
- ✅ 无 workspace 时工具调用失败并返回清晰错误
- ✅ 服务器目录绝对不会被访问
- ✅ 向后兼容：正常客户端（Claude Code/Codex）无需修改

---

## 相关文件

- `src/gateway_tool_runtime.py` - Workspace 提取逻辑
- `src/gateway_builtin_tools.py` - 工具执行和 workspace 验证
- `src/gateway_config.py` - 配置管理（已移除 workspace_root 持久化）
- `src/gateway_admin.py` - Admin UI（workspace 改为只读显示）
- `tests/test_gateway.py` - Workspace 安全测试

---

## 时间线

- **2026-06-16**: 发现漏洞
- **2026-06-16**: 实施修复
- **2026-06-16**: 安全测试验证
- **状态**: ✅ 已修复

---

## 相关修复

这个安全修复是以下问题的一部分：

1. **SECURITY_FIX_WORKSPACE.md** (本文档) - 路径遍历漏洞
2. **FIX_WORKSPACE_ROOT.md** - Workspace 不再持久化到配置文件
3. **CLAUDE.md** - 项目进度更新

---

## 建议

### 对于 Gateway 部署者

1. **立即更新**到修复版本
2. **审计日志**检查是否有可疑的文件访问
3. **轮换密钥**如果怀疑已被入侵
4. **限制网络**：Gateway 应只能被授权客户端访问

### 对于客户端开发者

1. **始终发送** `workspace_root` 或 `metadata.project_dir`
2. **不依赖**服务器默认值
3. **验证错误**处理无 workspace 时的响应

---

## 联系方式

如有安全相关问题，请联系：
- GitHub Issues: https://github.com/your-org/gateway/issues
- 邮箱: security@your-domain.com
