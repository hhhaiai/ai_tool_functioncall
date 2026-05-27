# 待完成工作

> 最后更新: 2026-05-27

## 高优先级

### 1. 高性能优化 (百亿 token/小时)
- [ ] 替换 ThreadingHTTPServer 为 asyncio/aiohttp
- [ ] 实现连接池复用 (当前连接池未实际使用)
- [ ] 添加 worker 进程支持
- [ ] 实现优雅关闭
- **预估**: 需要架构重构

### 2. gateway_protocol.py 剩余问题
- [ ] thinking/reasoning 文本错误注入为 system message
- [ ] Responses ↔ Anthropic 跨协议转换修复
- [ ] 图像内容静默丢弃
- [ ] 多个 system message 合并
- [ ] tool call 参数 JSON 解析失败处理

### 3. gateway_stats.py 查询优化
- [ ] 推送聚合到 SQL (当前全量加载到 Python)
- [ ] 添加复合索引
- [ ] CSV 导出字段转义
- [ ] get_hourly_trends 小时对齐修复

## 中优先级

### 4. Guardrails (输入/输出验证)
- [ ] PII 检测
- [ ] 提示注入过滤
- [ ] 内容审核集成
- [ ] 输出毒性过滤

### 5. 可观测性增强
- [ ] OpenTelemetry 集成
- [ ] Langfuse 导出
- [ ] 结构化日志
- [ ] 分布式追踪

### 6. 限流增强
- [ ] Per-user 速率限制
- [ ] Token-based 限制
- [ ] 团队配额管理

## 低优先级

### 7. 代码质量
- [ ] 拆分大文件 (gateway_builtin_tools.py: 1662 行)
- [ ] 提取 HTML 模板 (gateway_admin.py)
- [ ] 统一导入风格 (移除 `__import__`)

### 8. 测试增强
- [ ] 添加 property-based 测试
- [ ] 添加负载测试
- [ ] 添加混沌测试

### 9. 文档完善
- [ ] API 参考文档
- [ ] 部署指南更新
- [ ] 性能调优指南

## Go 项目 (远期)

### 10. Go 实现
- [ ] 设计 Go 项目结构
- [ ] 实现核心功能
- [ ] 编译为单一二进制
- [ ] 性能对比测试
- **前置条件**: Python 版本完全稳定

## 完成标准

每个功能必须满足:
1. ✅ 单元测试覆盖 80%+
2. ✅ 集成测试通过
3. ✅ 无 CRITICAL/HIGH 问题
4. ✅ 文档同步更新
5. ✅ 性能基准测试
