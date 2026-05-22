# 当前审计结论（2026-05-22）

本文件记录本轮对 `ai_tool_functioncall` 当前工作区的结构审计、风险点核验和回归修复结果。

## 1. 总体判断

当前工程定位与用户描述一致：它不是上游 API，也不是下游客户端，而是位于二者之间的 **Gateway 中游层**。

```text
上游：三方 LLM API
  - chat api：完全不支持 tool
  - sub api：部分支持 tool
  - full api：完全支持 tool

中游：本项目 Gateway
  - 三协议转换
  - 工具能力补齐
  - 上游能力配置
  - 上下文压缩 / 记忆 / fan-out

下游：Codex / Claude Code / DeepSeek-TUI / OpenCode / SDK / App
```

当前代码已经从旧的单体 `gateway_app.py` 拆成多个 `gateway_*` 模块。`gateway_app.py` 现在主要承担入口和旧 API 兼容重导出，不再应该承载大块核心逻辑。

## 2. 外部报告 8 个问题核验

| # | 外部报告问题 | 当前核验结论 | 证据 / 说明 |
|---|---|---|---|
| 1 | `UpstreamHTTPError` 重复定义 | 当前工作区已修 | 统一在 `src/gateway_errors.py`；`gateway_proxy.py`、`gateway_http_handler.py`、`gateway_tool_runtime.py` 均从统一模块导入。 |
| 2 | `SUPPORTED_PATHS` 等常量重复 | 当前工作区已修 | 统一在 `src/gateway_config.py`；HTTP handler/runtime 使用导入常量。 |
| 3 | 错误 payload / handler 相关重复 | 当前工作区已修 | `error_payload` 统一在 `gateway_errors.py`；HTTP 路由/Admin UI 归 `gateway_http_handler.py` / `gateway_admin.py`，本轮已删除 `gateway_tool_runtime.py` 中旧 HTTP/Admin 辅助代码副本。 |
| 4 | `_get_long_context_upstream()` 直接改 `os.environ` 有竞态 | 当前工作区不成立 | 当前函数直接构造 `NativeProxyClient(base_url/api_key/model)`，没有修改 `os.environ`。 |
| 5 | 流式文本 fallback 缺 `text_fallback` 标志 | 当前工作区已修 | `gateway_streaming.py` 识别文本工具调用后设置 `text_fallback=True`，并复用 runtime 的 `_append_text_tool_results()` / `_append_tool_results()` 回填路径；orchestrate-stream 调上游时强制非 stream。 |
| 6 | `.bak` 文件残留 | 真实存在，已清理 | 删除 `src/gateway_tool_runtime.py.bak`。 |
| 7 | 测试覆盖盲区 | 外部结论过时/夸大 | 当前有 131 个 unittest，覆盖协议转换、流式、工具编排、上下文 fan-out、SQLite 记忆、HTTP 路由、MCP、HTTP Action、鉴权、路径沙箱和 provider 失败语义等。仍可继续加强真实 provider 集成测试。 |
| 8 | `__getattr__` shim 每次 miss import，脆弱 | 当前工作区已修 | 已删除 `gateway_tool_runtime.py` 末尾动态 `__getattr__`；旧入口兼容由 `gateway_app.py` 的显式重导出和 module wrapper 承担。 |

## 3. 本轮实际发现并修复的当前回归

外部报告中部分问题是旧版本问题；当前工作区真正影响测试的是拆模块后的兼容回归。

已修复：

1. **旧单体 monkeypatch 不再同步到子模块**
   - 问题：测试和旧调用方会设置 `gateway.CONFIG_PATH`、`gateway.SQLITE_READY`、`gateway._gateway_config`，但拆模块后实际读取的是 `gateway_config` / `gateway_logging` / `gateway_tool_runtime` 内部变量。
   - 修复：`gateway_app.py` 增加兼容 module wrapper，将这些 legacy 全局转发到 owning modules。

2. **`gateway_builtin_tools._execute_tool_call()` 导入不存在的 `_execute_tool_call_impl`**
   - 问题：并行工具和部分 nested tool 调用失败。
   - 修复：改为调用 `gateway_tool_runtime._execute_tool_call()`。

3. **Responses `custom_tool_call` 文本 input 丢失原字段名**
   - 问题：`input: "40+2"` 被解析成 `text`，测试期望 `arguments["input"]`。
   - 修复：custom tool call 字符串 input 保留为 `{"input": ...}`。

4. **orchestrate streaming 模式把下游 `stream=true` 透给上游**
   - 问题：Gateway 自己负责下游 SSE 时，上游仍收到 `stream=true`。
   - 修复：orchestrate streaming 调上游时强制 `stream=false`，passthrough 模式不受影响。

5. **forced fan-out 用错 config 层级并缺少 forced strategy 标记**
   - 问题：上游返回 too-long 文案后，forced fan-out 没按完整 config 运行；策略标记也不符合契约。
   - 修复：传入完整 config；forced 时标记 `gateway_context.strategy=fanout_forced_synthesis`。

6. **MCP / Memory helper 仍引用旧 `app` 全局**
   - 问题：`mcp_list_tools`、`mcp_call_tool`、`Memory`、Agent helper 在拆模块后可能 `name 'app' is not defined`。
   - 修复：改为直接导入对应模块函数。

7. **MCP helper 名称被误判为 MCP public name**
   - 问题：`mcp_read_resource` 被解析成 server=`read`、tool=`resource`。
   - 修复：执行时内置工具优先；`_mcp_parse_public_name()` 只解析不歧义的 `mcp__server__tool`。

8. **SQLite 记忆 session key 不识别 `metadata.user_id` JSON 字符串**
   - 问题：同 session 记忆无法召回。
   - 修复：`_memory_session_key()` 解析 `metadata.user_id` 中的 JSON。

9. **超大记忆摘要缺少压缩标记**
   - 问题：巨型 turn 存入 SQLite 后看不出是 compacted memory。
   - 修复：超出摘要预算时加 `[gateway context compacted]` 标记。

10. **压缩后 local planner 又把请求膨胀回超大上下文**
   - 问题：`_maybe_compact_request_for_upstream()` 已删除大工具 schema / system 后，`_apply_local_planner_context()` 仍可能读取 `@src/` 并注入大量本地文件证据，导致上游请求重新超限。
   - 修复：当 `gateway_context.compacted=true` 时跳过 local planner 注入，优先保持压缩契约和上游上下文安全。

11. **POST 下游 API key 被吞掉后继续执行**
   - 问题：`GatewayHandler.do_POST()` 捕获 `_check_downstream_key()` 异常后没有中断，导致受保护 POST 路由可能继续执行。
   - 修复：POST 鉴权失败直接抛出 `DownstreamAuthError`，统一映射为 401；新增回归测试。

12. **workspace 路径可被绝对路径 / `..` 逃逸**
   - 问题：`_resolve_workspace_path()` 对绝对路径和相对逃逸校验不够，文件工具存在越界风险。
   - 修复：统一 resolve 到 `GATEWAY_WORKSPACE_ROOT` 下，并用 `relative_to(root)` 强制校验；越界返回 `permission_denied`；新增回归测试。

13. **`image_generation` provider 失败时曾伪造本地 placeholder 成功**
   - 问题：真实图片 provider 全部失败后仍可能返回 `ok:true/provider=local_placeholder`。
   - 修复：移除 placeholder 分支；只调用真实 OpenAI / Pollinations / Hugging Face provider，全部失败时返回失败并由 Gateway 标记 `connector_required`；新增回归测试。

14. **脚本直接入口在模块拆分后失效**
   - 问题：`python3 src/toolcall_gateway.py` 使用包相对导入时会失败。
   - 修复：直接执行时把项目根加入 `sys.path` 并导入 `src.gateway_app`；已同时验证脚本和 `python -m` 入口。

15. **上游协议环境变量命名不一致**
   - 问题：部分文档/compose 使用 `UPSTREAM_PROTOCOL`，代码使用 `GATEWAY_UPSTREAM_PROTOCOL`，容易静默配置错。
   - 修复：`gateway_config._env_upstream_protocol()` 同时支持当前 `GATEWAY_UPSTREAM_PROTOCOL` 和 legacy `UPSTREAM_PROTOCOL`，且当前变量优先；脚本、proxy、runtime、streaming 统一走 helper；新增回归测试。

16. **Docker / compose 默认运行参数不一致**
   - 问题：容器日志环境变量和监听地址与当前代码/部署预期不完全一致。
   - 修复：Docker 默认使用 `GATEWAY_SQLITE_LOG_PATH`，CMD 监听 `0.0.0.0:8885`；compose 同时传递当前和 legacy upstream protocol 环境变量。

17. **生产 compose 和公开示例默认权限过宽**
   - 问题：Docker/compose 示例曾默认开启 write/shell 工具，且生产 compose 没有强制要求 admin/downstream/upstream secret。
   - 修复：compose 和 `.env.example` 默认 `GATEWAY_ALLOW_WRITE_TOOLS=0`、`GATEWAY_ALLOW_SHELL_TOOLS=0`；生产 compose 对 `UPSTREAM_*`、`GATEWAY_ADMIN_PASSWORD`、`GATEWAY_DOWNSTREAM_KEY` 使用必填变量；部署文档同步最小权限策略。

18. **DOWNSTREAM_API_KEY 与 GATEWAY_DOWNSTREAM_KEY 行为不一致**
   - 问题：`DOWNSTREAM_API_KEY` 会出现在客户端配置片段，但未自动生成下游鉴权 key，容易生成“能复制但不能认证”的配置。
   - 修复：`_default_config()` 同时接受 `GATEWAY_DOWNSTREAM_KEY` 和 `DOWNSTREAM_API_KEY` 创建下游 key；客户端片段优先显示 `DOWNSTREAM_API_KEY`，否则显示 `GATEWAY_DOWNSTREAM_KEY`；新增回归测试。

19. **DeletePath 可递归删除 workspace root**
   - 问题：在 write tools 已开启且传入 `recursive=true` 时，`DeletePath path=.` 会删除整个 workspace root。
   - 修复：显式拒绝删除 workspace root，返回 `permission_denied`；新增回归测试。

## 4. 当前验证结果

```bash
python3 -m py_compile $(find src tests -name '*.py' -type f | sort)
# OK

bash -n scripts/mimo_gateway.sh scripts/deploy.sh scripts/generate-ssl.sh scripts/claude_m1.sh scripts/install_deps.sh
# OK

python3 -m unittest discover -s tests -v
# Ran 131 tests ... OK

# HTTP/UI smoke
# GET /, /healthz, /ui, /client-config.json, /client-config
# OK; /healthz builtin_tool_count=67
```

## 5. 仍建议后续处理

1. **提交 GitHub 前安全闸口**
   - 已清理远程 URL 中的嵌入式 token，并通过 `.gitignore` / `.dockerignore` 排除本地配置、SQLite、runtime、trace、workspace、SSL 私钥等运行数据。提交前仍需以 `git ls-files --cached --others --exclude-standard` 为候选集做 secret scan。

2. **增加真实 provider smoke**
   - unittest 已覆盖 fake upstream 和核心逻辑；真实三方 provider 的 native-tools/weak-tools 差异仍建议用带凭据的集成脚本周期性验证。

3. **继续减少历史文档漂移**
   - README 和核心 docs 已按当前实现同步；更早的专题分析文档仍应在后续改功能时顺手核对。
