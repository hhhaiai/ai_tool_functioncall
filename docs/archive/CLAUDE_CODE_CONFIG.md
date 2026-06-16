# Claude Code 接入配置

## Gateway 服务状态

✅ **Gateway 已启动**: http://127.0.0.1:8885  
✅ **上游服务**: http://127.0.0.1:3000 (gpt-5.5)  
✅ **HTTP 兼容性修复**: 使用 curl 后端替代 urllib  
✅ **工具编排修复**: 4个核心测试通过  

## Claude Code 环境变量配置

```bash
claude_aidebug() {
    export ANTHROPIC_BASE_URL="http://127.0.0.1:8885"
    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
    export ANTHROPIC_AUTH_TOKEN="sk-sanbo"
    export ANTHROPIC_API_KEY=""
    # version 2.x.x
    export ANTHROPIC_DEFAULT_OPUS_MODEL="gpt-5.5"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="gpt-5.5"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="gpt-5.5"
    # version 1.x.x
    export ANTHROPIC_MODEL="gpt-5.5"
    export ANTHROPIC_SMALL_FAST_MODEL="gpt-5.5"
    export ENABLE_LSP_TOOL="1"
    /Users/sanbo/.local/bin/claude-tap --dangerously-skip-permissions "$@"
}
```

## 测试步骤

1. **启动 Gateway** (已自动启动)
   ```bash
   python3 -m src.gateway_app
   # 监听在 http://127.0.0.1:8885
   ```

2. **测试基本对话**
   ```bash
   curl http://127.0.0.1:8885/v1/chat/completions \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer sk-sanbo" \
     -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"你好"}]}'
   ```

3. **使用 Claude Code 测试**
   ```bash
   # 加载环境变量
   claude_aidebug
   
   # 在新终端测试对话
   # Gateway 会自动处理工具调用（如果上游支持）
   ```

## 注意事项

### 上游工具支持情况

当前上游 (127.0.0.1:3000 gpt-5.5) **不支持原生 tool calls**：
- 发送工具定义时，模型直接回答问题而不调用工具
- 这是上游模型的限制，不是 Gateway 的问题

**Gateway 的价值**：
- 即使上游不支持工具，Gateway 可以为 Claude Code 提供统一接口
- 未来如果上游支持工具调用，Gateway 会自动启用工具编排功能
- Gateway 提供的内置工具（文件读写、命令执行等）仍可工作

### 监控日志

```bash
# 查看 Gateway 实时日志
tail -f gateway_server.log

# 查看请求统计
curl -u admin:admin http://127.0.0.1:8885/admin/stats.json | jq '.'
```

### 停止服务

```bash
# 停止 Gateway
kill $(cat gateway_server.pid)

# 或强制停止
lsof -ti:8885 | xargs kill -9
```

## 故障排除

### 问题：Gateway 返回 "Remote end closed connection"

**原因**: Python urllib 与某些服务器不兼容  
**解决**: 已修复，使用 curl 作为 HTTP 后端

### 问题：上游不调用工具

**原因**: 上游模型本身不支持 function calling  
**验证**: 直接 curl 上游测试是否返回 tool_calls

### 问题：Claude Code 连接失败

**检查清单**:
1. Gateway 是否启动：`curl http://127.0.0.1:8885/`
2. 环境变量是否设置：`echo $ANTHROPIC_BASE_URL`
3. 认证token是否正确：`sk-sanbo`

## 下一步测试

1. 在终端运行 `claude_aidebug`
2. 尝试简单对话验证连接
3. 观察 Gateway 日志查看请求流转
4. 测试文件操作、代码分析等功能
