# CRITICAL/HIGH 问题修复记录

> 修复日期: 2026-05-27
> 审查方式: 8 个并行 agent 逐行代码审查 + 协议层专项审查

## CRITICAL 修复 (10 项)

### 1. EXEC_SESSIONS 未定义导致 NameError
- **文件**: `gateway_builtin_tools.py`
- **问题**: `EXEC_SESSIONS` 和 `EXEC_SESSIONS_LOCK` 被使用但从未定义
- **影响**: 所有 exec_shell_start/write_stdin/exec_wait/exec_kill 工具调用崩溃
- **修复**: 在模块级别添加定义

### 2. 路径遍历漏洞
- **文件**: `gateway_claude_compat.py`
- **问题**: `_resolve_path` 不验证路径是否在 workspace_root 内
- **影响**: 攻击者可读写服务器任意文件 (如 `/etc/passwd`)
- **修复**: 添加 `resolved.relative_to(root)` 包含检查

### 3. Bash 工具无安全检查
- **文件**: `gateway_claude_compat.py`
- **问题**: `_execute_bash` 直接执行用户命令，无 shell_enabled 检查
- **影响**: 下游客户端可执行任意命令
- **修复**: 添加 `tools.shell_enabled` 配置检查

### 4. SSRF 漏洞
- **文件**: `gateway_claude_compat.py`
- **问题**: `_execute_web_fetch` 无 URL 验证
- **影响**: 可探测内部服务 (云元数据、Redis 等)
- **修复**: 添加 `_validate_url_not_private()` 阻止私有/回环 IP

### 5. 配置参数被忽略
- **文件**: `gateway_intelligence.py`
- **问题**: `max_decomposition_parts` 配置被硬编码 `max_parts=5` 忽略
- **影响**: 用户配置不生效
- **修复**: 通过函数链传递 config 参数

### 6. 无界缓存导致 OOM
- **文件**: `gateway_context.py`
- **问题**: `_SUMMARY_CACHE` 无大小限制、无线程安全
- **影响**: 高并发下内存耗尽，进程被 OOM killer 终止
- **修复**: 替换为有界 `OrderedDict` (max 512)，线程安全 helper，`hashlib.sha256` 稳定 hash

### 7. 缓存锁持有期间计算嵌入向量
- **文件**: `gateway_cache.py`
- **问题**: `SemanticCache.get()` 在锁内调用 `embedding_provider.embed()`
- **影响**: 远程嵌入服务阻塞时，所有缓存操作被阻塞
- **修复**: 将 `embed()` 调用移到锁外

### 8. 远程嵌入服务异常被静默吞没
- **文件**: `gateway_cache.py`
- **问题**: `RemoteEmbeddingProvider.embed()` 捕获所有异常但不记录
- **影响**: 嵌入服务故障时无任何日志，静默降级
- **修复**: 添加 `logging.warning()` 和缓存的 fallback provider

### 9. 连接池计数错误
- **文件**: `gateway_concurrency.py`
- **问题**: 池化连接不增加 `_active_count`，导致连接限制失效
- **影响**: 实际连接数可远超配置限制
- **修复**: 统一 `get_connection`/`release_connection`，所有获取都增加计数

### 10. 请求队列内存泄漏
- **文件**: `gateway_concurrency.py`
- **问题**: `RequestQueue._queue` 只增不减
- **影响**: 高吞吐下内存无限增长
- **修复**: 移除无界队列，请求直接提交到 executor

## HIGH 修复 (5 项)

### 11. LoadBalancer 健康状态竞态条件
- **文件**: `gateway_concurrency.py`
- **问题**: `report_success`/`report_failure`/`check_health` 无锁保护
- **影响**: 并发下健康状态不一致
- **修复**: 所有健康状态变更使用 `self._lock`

### 12. least_connections 策略使用错误指标
- **文件**: `gateway_concurrency.py`
- **问题**: 使用 `consecutive_failures` 而非活跃连接数
- **修复**: 添加 `active_connections` 字段，用作排序键

### 13. create_upstream_pool 忽略配置
- **文件**: `gateway_concurrency.py`
- **问题**: config 参数被接受但从未使用
- **修复**: 解析 config dict 并传递给 MultiUpstreamManager

### 14. MultiUpstreamManager 修改调用者列表
- **文件**: `gateway_concurrency.py`
- **问题**: `__init__` 直接引用传入列表
- **修复**: 使用 `list(upstreams)` 防御性拷贝

### 15. 缺少输入验证
- **文件**: `gateway_cache.py`
- **问题**: `query` 参数无类型检查
- **修复**: 添加 `isinstance(query, str)` 检查

## gateway_protocol.py 修复 (5 项)

> 修复日期: 2026-05-27 (第二批)

### 16. [HIGH] thinking 文本替换 system prompt
- **文件**: `gateway_protocol.py`
- **问题**: `_convert_anthropic_messages_to_openai` 将 reasoning_text 和 system_text 混用同一个返回槽位，`reasoning_text or system_text` 导致 thinking 内容覆盖实际系统提示
- **影响**: 多轮 Anthropic 对话中的 thinking 内容成为 system prompt，丢失真实系统指令
- **修复**: 返回 3-tuple `(messages, system_text, reasoning_text)` 分离两者

### 17. [HIGH] Responses ↔ Anthropic 跨协议转换缺失
- **文件**: `gateway_protocol.py`
- **问题**: `_convert_response_to_downstream` 中 Anthropic→Responses 和 Responses→Anthropic 两条路径只转换到 Chat 中间格式，未链式转换到目标格式
- **影响**: Responses 客户端用 Anthropic 上游收到 Chat 格式，Anthropic 客户端用 Responses 上游收到 Chat 格式
- **修复**: 链式转换 Anthropic→Chat→Responses 和 Responses→Chat→Anthropic

### 18. [MEDIUM] 图像内容静默丢弃
- **文件**: `gateway_protocol.py`
- **问题**: Anthropic image 块被替换为 `"[image]"` 字符串，base64/URL 数据丢失
- **影响**: 多模态对话转发到 OpenAI 时丢失所有图像
- **修复**: 转换为 OpenAI `image_url` 格式 (base64 data URL 和 URL 引用)

### 19. [MEDIUM] 多个 system message 只保留最后一个
- **文件**: `gateway_protocol.py`
- **问题**: `_convert_anthropic_messages_to_openai` 和 `_openai_messages_to_anthropic` 中每个 system message 覆盖前一个
- **影响**: 拆分到多个 system message 的客户端丢失前面的指令
- **修复**: 使用 `system_parts` 列表累积，最终 `"\n".join(system_parts)` 合并

### 20. [MEDIUM] Tool call JSON 解析失败静默吞没
- **文件**: `gateway_protocol.py`
- **问题**: `json.JSONDecodeError` 时 args 被替换为 `{}`，无日志、无错误传播
- **影响**: 截断的 JSON 参数变为 `{}`，工具执行失败且无法调试
- **修复**: 添加 `_logger.warning()` 记录工具名和原始参数
