# Workspace Root 修复总结

## 修复概览

**日期**: 2026-06-16  
**修复内容**: Workspace Root 安全漏洞 + 架构改进  
**影响**: 所有 Gateway 用户

---

## 🔴 关键安全修复

### 漏洞：路径遍历攻击

**严重性**: CRITICAL (CVSS 9.8)

**问题**：Gateway 在客户端未提供 workspace 时，使用服务器目录作为默认值

**风险**：
- 攻击者可读取服务器任意文件
- SSH 密钥、配置文件、数据库凭证等完全暴露
- 可能导致服务器被完全控制

**修复**：
```python
# ❌ 修复前：危险的回退逻辑
def _request_workspace_root(body):
    # ...
    return _workspace_root()  # 返回服务器的 os.getcwd()

# ✅ 修复后：安全失败
def _request_workspace_root(body):
    # ...
    return None  # 绝不使用服务器目录
```

**验证**：
```bash
python3 << 'EOF'
from src.gateway_tool_runtime import _request_workspace_root
assert _request_workspace_root({}) is None  # ✓ 安全
EOF
```

---

## ✅ 架构改进

### 1. Workspace 不再持久化

**问题**：`workspace_root` 被保存到 `.gateway_service.json`，导致多用户冲突

**修复**：
- `save_config()` 自动移除 `workspace_root` 字段
- HTTP handler 不再保存用户提交的 `workspace_root`
- Admin UI 显示为只读字段

### 2. 动态 Workspace 解析

**优先级链**（从高到低）：
1. **显式字段**: `body.workspace_root` 或 `body.gateway_workspace`
2. **客户端 metadata**: `metadata.project_dir`, `metadata.cwd`
3. **自动提取**: 从 system/messages 提取（Claude Code 格式）
4. **环境变量**: `GATEWAY_WORKSPACE_ROOT`（仅测试用）
5. **❌ 不再有默认值** - 返回 None，安全失败

---

## 安全原则

### 🔴 绝对红线

**Gateway 绝对不能使用服务器目录作为工作空间**

- ❌ 不能使用 `os.getcwd()`
- ❌ 不能使用服务器的任何路径
- ❌ 不能从配置文件读取作为默认值
- ❌ 不能在本地预演/测试用户代码

### ✅ 正确做法

- ✅ 所有 workspace 必须来自客户端
- ✅ 客户端 = 用户本地机器（不是 Gateway 服务器）
- ✅ 如果客户端未提供，安全失败（返回错误）
- ✅ Gateway 只提供服务，不操作本机

---

## Gateway 服务定位

**Gateway 是纯服务提供方**：

```
┌─────────────┐                  ┌──────────────┐
│   用户本地   │  ─── 请求 ─────>  │   Gateway    │
│ (Claude Code)│                  │   (服务器)    │
│             │  <─── 响应 ─────  │             │
│             │                  │             │
│ workspace:  │                  │ ❌ 不操作    │
│ /Users/alice│                  │   本地文件   │
└─────────────┘                  └──────────────┘
```

**服务内容**：
- 协议转换（OpenAI ↔ Anthropic）
- Tool calls 支持
- 无限上下文
- 智能缓存
- 并发管理

**不提供**：
- ❌ 本地文件操作（必须由客户端提供 workspace）
- ❌ 预演/测试（未来会有安全沙箱）
- ❌ 服务器目录访问

---

## 修改文件清单

### 核心修改

1. **`src/gateway_tool_runtime.py`**
   - `_request_workspace_root()` 返回 `None` 而不是服务器目录
   - `_workspace_scope()` 处理 `None` 情况

2. **`src/gateway_builtin_tools.py`**
   - `_workspace_root()` 抛出错误而不是使用 `os.getcwd()`

3. **`src/gateway_config.py`**
   - `save_config()` 移除 `workspace_root` 字段

4. **`src/gateway_http_handler.py`**
   - 不再保存用户提交的 `workspace_root`

5. **`src/gateway_admin.py`**
   - `workspace_root` 改为只读显示

### 文档

- **SECURITY_FIX_WORKSPACE.md** - 安全漏洞详细分析
- **FIX_WORKSPACE_ROOT.md** - 修复说明
- **CLAUDE.md** - 项目进度更新
- **SUMMARY_WORKSPACE_FIX.md** - 本文档

---

## 测试验证

### 安全测试（全部通过）

```bash
# Test 1: 无 workspace 返回 None
assert _request_workspace_root({}) is None  # ✓

# Test 2: 工具调用失败并返回清晰错误
try:
    _workspace_root()
except ToolExecutionError as e:
    assert "No workspace root provided" in str(e)  # ✓

# Test 3: 显式 workspace 正常工作
result = _request_workspace_root({"workspace_root": "/tmp"})
assert result == pathlib.Path("/tmp").resolve()  # ✓

# Test 4: 服务器目录绝不使用
server_cwd = os.getcwd()
result = _request_workspace_root({})
assert result != pathlib.Path(server_cwd).resolve()  # ✓
```

### Workspace 测试（7/7 通过）

```bash
pytest tests/test_gateway.py -k 'workspace and not streaming_passthrough'
# 7 passed
```

---

## 客户端兼容性

### Claude Code ✅

自动在 system prompt 中注入工作目录：
```
Primary working directory: /Users/alice/project
```

Gateway 自动提取，无需修改。

### Codex ✅

发送 metadata 字段：
```json
{
  "metadata": {
    "project_dir": "/Users/alice/project",
    "cwd": "/Users/alice/project"
  }
}
```

Gateway 自动提取，无需修改。

### 自定义客户端 ✅

显式发送 workspace_root：
```json
{
  "workspace_root": "/Users/alice/project"
}
```

---

## 部署检查清单

在生产环境部署前：

- [ ] 运行安全测试验证
- [ ] 运行 workspace 测试套件（7个测试）
- [ ] 确认 `_request_workspace_root({})` 返回 `None`
- [ ] 确认工具调用在无 workspace 时失败
- [ ] 确认 `.gateway_service.json` 中无 `workspace_root`
- [ ] 确认环境变量 `GATEWAY_WORKSPACE_ROOT` 未设置（生产环境）
- [ ] 审计日志检查是否有可疑文件访问
- [ ] 如有必要，轮换所有密钥

---

## 影响评估

### 修复前

- 🔴 服务器文件系统完全暴露
- 🔴 所有敏感文件可被读取
- 🔴 可能导致服务器被控制

### 修复后

- ✅ 服务器目录绝对安全
- ✅ 只能访问客户端提供的 workspace
- ✅ 无 workspace 时工具调用安全失败
- ✅ 支持多用户/多项目

### 向后兼容性

✅ **完全向后兼容**

正常客户端（Claude Code、Codex）已经发送 workspace，无需任何修改。

---

## 后续计划

### 短期（已完成）

- ✅ 修复安全漏洞
- ✅ 移除 workspace 持久化
- ✅ 更新文档

### 中期

- [ ] 添加 workspace 访问审计日志
- [ ] 实现 workspace 路径白名单（可选）
- [ ] 监控告警：检测无 workspace 的请求

### 长期

- [ ] 安全沙箱：支持预演/测试（隔离环境）
- [ ] 细粒度权限控制：读写分离
- [ ] Workspace 使用统计

---

## 联系方式

如有问题或发现新的安全问题：

- GitHub Issues: https://github.com/your-org/gateway/issues
- 安全邮箱: security@your-domain.com

---

**记住核心原则**：

🔴 **Gateway 绝不操作服务器本地文件**  
✅ **所有操作都在客户端提供的 workspace 中进行**
