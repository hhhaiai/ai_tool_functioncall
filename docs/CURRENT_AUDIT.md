# 当前审计结论（2026-06-19）

本文件记录本轮对 `ai_tool_functioncall` 当前工作区的结构审计、风险点核验和回归修复结果。

## 2026-06-19 增量审计结论

本轮重新按用户确认的 Gateway 目标检查：

```text
上游：普通/弱工具 API，可完全不支持 tools/function calls
中游：本 Gateway，做协议适配、文本工具适配、workspace 隔离、上下文/记忆治理
下游：Claude Code / Codex / SDK，必须拿到协议正确的 tool/function 处理结果
```

发现并修复的当前阻断点：
1. macOS `urllib` 系统代理会把 `127.0.0.1` mock upstream / Admin / Web2API 请求送到代理，导致大量 `RemoteDisconnected`；新增 package bootstrap 统一绕过 loopback proxy。
2. 弱上游 adapter 曾对无 tools 的普通请求也注入完整工具手册，导致简单 `/anthropic/v1/messages` 被污染；现在只有下游实际提交 tools/tool_choice 或压缩前请求含 tools 时才注入 adapter。
3. workspace 默认值曾在配置层回填服务 cwd，和“不能把 Gateway 服务目录当用户项目目录”的边界冲突；现在默认空 root，优先请求/metadata/env/显式配置，最后匿名隔离空间。
4. Admin UI 无 workspace 时调用 Skills 扫描会 500；现在无 workspace 时跳过项目 skills，仅展示用户全局/额外 skills。
5. 配置 hash 被二次加密导致保存值不稳定；现在 `password_hash` / `key_hash` 保持稳定 hash，明文密码仍不落盘。
6. 匿名 workspace 未解析 `metadata.user_id` JSON 内 session，导致同 session 记忆召回跨 workspace 失效；现在按 session 稳定。
7. Bash 文本工具参数归一化只保留单一字段，弱 markup 测试会在 `command/cmd` 间不一致；现在两个字段兼容。
8. 工具执行位置曾混在一起：Read/LS/Bash/Skill 等用户机器工具可能被 Gateway 服务机执行。现在按工具归属分流：gateway-owned（HTTP Action/MCP/WebFetch/WebSearch/calculator/Memory 等）由 Gateway 真执行；用户侧文件/shell/GUI/local-agent/Skill 工具默认返回下游原生 `tool_use/tool_calls/function_call`，由 Claude Code/Codex 在用户机器执行。

当前门禁：
```bash
python3 -m compileall -q src tests
# OK
python3 -m pytest -q
# 886 passed, 2 skipped

local mock smoke（临时 127.0.0.1:9011/8899）
# OK: /healthz, /v1/models, /v1/chat/completions, /v1/tools/call calculator, /v1/messages user-side LS delegation
```

---

补充运行验证：当前工作区支持 Claude Code / Anthropic SDK 常见的
`ANTHROPIC_BASE_URL=http://127.0.0.1:8885/anthropic` 接入方式；
`/anthropic/v1/messages` 与 `/anthropic/v1/messages/count_tokens` 会在 HTTP 入口规范化为
`/v1/messages` 与 `/v1/messages/count_tokens`，下游 key 可通过
`Authorization: Bearer <key>` 或 `x-api-key: <key>` 提交。

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
| 7 | 测试覆盖盲区 | 外部结论过时/夸大 | 当前有 200 个 pytest 回归测试，覆盖协议转换、流式、工具编排、上下文 fan-out、SQLite 记忆、HTTP 路由、MCP、HTTP Action、鉴权、路径沙箱、provider 失败语义、Claude Code/Codex 项目根识别、Skills/plugin/.traces/Memory 隔离、streaming passthrough 内部字段剥离等。仍可继续加强真实 provider 集成测试。 |
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

20. **配置模板的开发机路径与高权限默认不适合公开发布**
   - 问题：`gateway.config.*` 曾包含本机绝对路径并默认开启 write/shell，复制模板后容易把高危工具暴露给非可信部署。
   - 修复：模板统一使用 `./workspace`，默认关闭 `allow_write_tools` / `allow_shell_tools`；Docker 镜像不再注入 `GATEWAY_ADMIN_PASSWORD=admin`，开发 compose 的空密码环境保持 must-change 语义；新增模板安全默认回归测试。

21. **`admin.password` 模板字段与真实认证逻辑不一致**
   - 问题：公开模板和运行文档展示 `admin.password`，但 runtime 只校验 `password_hash`，用户修改明文字段可能以为已改密码。
   - 修复：`load_config()` / `save_config()` 会把 `admin.password` 归一化为 `password_hash` 且不回写明文；已有 hash 优先；新增回归测试。

22. **客户端配置片段里的 API Key 可能不可认证**
   - 问题：只设置 `gateway.client_snippet_api_key` 时，`/client-config` 会生成下游片段，但 `downstream_keys` 可能没有对应 hash。
   - 修复：保存/加载配置时自动为 `client_snippet_api_key` 生成或更新 `client-snippet` downstream key；新增端到端回归，验证复制出的 key 可调用受保护 `/v1/tools/call`。

23. **损坏配置文件会静默回退默认值，存在 fail-open 风险**
   - 问题：`.gateway_service.json` 已存在但 JSON 损坏或根节点不是对象时，`load_config()` 曾吞掉异常并按默认配置继续运行，可能重新打开开发默认 `admin/admin` 或跳过下游 key。
   - 修复：新增 `ConfigError`，坏配置返回结构化 500 并 fail closed；Admin/API 入口均覆盖回归测试；认证用户名、管理员密码 hash、downstream key hash 使用 constant-time bytes 比较。

24. **日志和 Admin 配置展示的敏感字段遮盖范围过窄**
   - 问题：请求/响应日志和 Admin redacted config 只遮盖少数字段，可能漏掉 nested `X-API-Key`、`Cookie`、token、secret、password、`key_hash`、long-context upstream key 或 HTTP Action secret。
   - 修复：新增共享递归 redaction helper；日志和 Admin 配置展示复用同一套字段规则；保留 `must_change_password` 等非敏感状态字段；新增回归测试覆盖 nested headers、token/secret/password/cookie/key_hash。

25. **Admin POST 缺少浏览器跨源写操作防护**
   - 问题：Admin 写接口只有 Basic Auth。浏览器已缓存管理员凭据时，跨源页面可尝试提交表单触发配置变更，存在 CSRF 风险。
   - 修复：Admin POST 在读取表单/写配置前校验 `Origin` / `Referer`；跨源和畸形来源返回 403 且不变更配置；同源请求和无来源头的 CLI/脚本请求保持兼容；新增 3 条回归测试。

26. **HTTP Action 执行契约与失败语义不一致**
   - 问题：HTTP Action 文档承诺 `GET` / `DELETE` 使用 query、`headers` 支持 `${ENV}`、`max_bytes` 限制响应、HTTP/URL 错误记录为 tool failure；旧实现总是 JSON body、header 不展开、失败以成功 tool result 返回，且 POST action 默认重试会重复触发外部副作用。
   - 修复：HTTP Action 现在只允许绝对 `http(s)` URL；`GET` / `DELETE` 把 arguments 追加到 query；header 配置支持 `${ENV}` 展开，query 参数只做 JSON-safe 字符串化；成功和错误响应均执行 `max_bytes` 上限；HTTP 4xx/5xx、连接失败、非法 URL、响应超限都走 `ToolExecutionError` 并写入 `tool_failures`；HTTP Action 默认不重试，只有 action 显式 `max_retries` 才重试；新增 4 条回归测试。

27. **HTTP POST 请求体缺少读取前上限**
   - 问题：`_read_json()` / `_read_form()` 直接按 `Content-Length` 把请求体读入内存。即使后续上下文压缩能处理大 prompt，恶意或误配置的大请求也会在进入业务逻辑前消耗网关内存；Admin form 也可能在校验前读取超大 body。
   - 修复：新增 `gateway.max_request_body_bytes` / `GATEWAY_MAX_REQUEST_BODY_BYTES`，默认 64MB；API JSON 与 Admin form 共用 `_read_limited_body()`，超限在读取前返回结构化 413 `request body too large`，并避免 Admin 配置被修改；新增 2 条回归测试。

28. **受保护 API POST 在鉴权前解析 body**
   - 问题：`/v1/*` 与 direct tool POST 路由先 `_read_json()` 再 `_check_downstream_key()`；当配置了下游 key 时，未授权请求仍会触发 JSON 解析、请求体大小检查和潜在 500/413，增加无效请求的资源消耗与错误面。
   - 修复：受保护 API POST 现在先校验 downstream key，再读取/解析 JSON body；未授权 malformed JSON 和 oversized body 都稳定返回 401，不再进入 body 解析路径；扩展下游鉴权回归测试覆盖该顺序。

29. **请求/响应日志 payload 无截断上限**
   - 问题：`_write_request_log()` 对 request/response 只做敏感字段遮盖，然后原样写入 SQLite/JSONL。长 prompt、fan-out 结果或大工具响应会让 `request_logs` 快速膨胀，也会让 Admin tail 查询变重。
   - 修复：新增 `gateway.max_log_payload_chars` / `GATEWAY_MAX_LOG_PAYLOAD_CHARS`，默认 200000；日志先 redaction 后按方向截断，并保留 `gateway_truncated`、原始长度、截断预算和预览信息；公开模板/compose 同步暴露该配置；新增回归测试覆盖 SQLite tail 返回截断摘要而非原始大文本。

30. **tool failure 内容未统一脱敏/截断**
   - 问题：`tool_failures.content` 由各调用点手动截断，低层记录入口没有统一脱敏/封顶；HTTP Action、MCP 或工具异常详情可能把长响应、错误页面或文本 token 写入 SQLite/JSONL。
   - 修复：`_record_tool_failure()` 与 legacy failure import 现在统一先遮盖文本中的 Authorization/API key/token/secret/password/cookie，再复用 `gateway.max_log_payload_chars` 截断；调用点不再各自 `[:1000]`；新增回归测试覆盖失败内容不泄露 secret 且不落入原始大文本。

31. **Admin 数字字段非法值会触发 500 或部分写入风险**
   - 问题：`/admin/config`、`/admin/upstream-profile`、`/admin/client-config` 的数字字段原先分散使用 `int()` / `float()`；非法值可能冒泡到 500，且在保存前已经修改内存中的部分配置对象。
   - 修复：新增统一 `_admin_form_int()` / `_admin_form_float()`；非法数字统一返回 400 `invalid numeric field: <field>`，并在 `save_config()` 前停止，保证配置文件不发生部分写入；新增回归测试覆盖 gateway/context/client/upstream/profile 数字字段。

32. **Admin 部分表单提交会把已有数字配置重置为默认值**
   - 问题：缺失数字字段曾 fallback 到硬编码默认值，而不是已有配置；脚本或旧客户端做部分 POST 时可能重置 `max_concurrent_requests`、context fanout 或 client token limit 等值。
   - 修复：数字解析优先级改为“提交值 > 旧配置 > 默认值”；空字符串视为缺失；新增回归测试覆盖 `/admin/config` 与 `/admin/client-config` 缺字段保留旧值。

33. **`gateway.max_tool_rounds` 保存后未被 runtime 使用**
   - 问题：Admin UI/配置文件可保存 `gateway.max_tool_rounds`，但非流式和流式 runtime 只读 `GATEWAY_MAX_TOOL_ROUNDS` 环境变量，导致 UI 保存值不生效。
   - 修复：新增 `_configured_max_tool_rounds()`，运行时优先级为环境变量 > `gateway.max_tool_rounds` > 默认 5；非流式和流式编排共用；新增 FakeClient 回归测试证明配置值会限制工具循环。

34. **Claude Code `/anthropic` base URL 与严格 Anthropic response shape**
   - 问题：Claude Code 使用 `ANTHROPIC_BASE_URL=http://127.0.0.1:8885/anthropic` 时会请求 `/anthropic/v1/messages`，旧路由只支持 `/v1/messages`；同时 OpenAI Chat 上游转换回 Anthropic Messages 时必须返回严格 `type=message`、`role=assistant`、`stop_reason=end_turn` 等字段。
   - 修复：新增 `/anthropic` 兼容前缀规范化；下游鉴权支持 `x-api-key`；OpenAI Chat -> Anthropic Messages 响应统一补齐 strict shape；客户端配置片段生成 `claude_mnative()`；新增回归测试覆盖路由、token count、x-api-key 和 response shape。

35. **Admin UI 缺少上游模型自动获取与 capability 可视化**
   - 问题：上游模型需要手填，tools/vision/function-call 等能力配置不够直观，容易把不支持 native tools 的上游误配置成可发原生 schema。
   - 修复：Admin UI 重做为卡片化深色界面，直接展示/保存 tools、function calls、parallel tools、vision、streaming、JSON schema 等能力；新增 `/admin/upstream-models.json` 从真实上游 `/v1/models` 拉取模型并填充 datalist；新增回归测试覆盖 UI 关键元素和模型接口鉴权/上游鉴权。

36. **`tools_enabled=auto` 未结合上游 capability 做运行时降级**
   - 问题：即使 profile 标记 `supports_tools=false` / `supports_function_calls=false`，`auto` 模式仍可能向上游发送原生 `tools` schema。
   - 修复：`_merge_builtin_tools()` 现在读取 active upstream capabilities；`auto` + native tools/function calls 关闭时自动走文本工具适配，并按工具归属执行 gateway-owned 工具或下发用户侧工具；`native_only` + 能力关闭时 fail-fast；新增回归测试覆盖该路径。

37. **弱上游文本工具适配遇到 Claude Code 大 harness 会触发 provider `too long`**
   - 问题：Claude Code 请求可携带大 system、system-reminder、skills 上下文和几十个工具 schema；当上游不支持 native tools、Gateway 改用文本工具适配时，如果不先压缩，真实上游可能 200 返回 `Sorry, the text you sent is too long!`。
   - 修复：新增 `gateway.text_tool_adapter_compact_token_limit` / `GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT`；阈值动态计算为 `max(8000, min(upstream.max_input_tokens * 0.45, config_cap))`，config_cap 默认 48000，设为 0 可关闭；文本工具适配前会去掉 native tools、压缩 system/reminder 大块，并注入 `[gateway context compacted]` 标记；新增回归测试证明发送给上游的 payload 低于预算且保留用户意图。

38. **用户声明的 `calc` / `expr` 工具与内置 calculator 不兼容**
   - 问题：用户按 Anthropic tools 示例声明 `name=calc`、参数 `expr`，而 Gateway 内置工具名是 `calculator`、参数是 `expression`；直接工具调用或模型返回 `calc` 时会落入 tool_not_found 或参数不匹配。
   - 修复：`calculator` 新增 `calc` alias，参数归一化新增 `expr -> expression`；已用 `/v1/tools/call` 和 `/v1/functions/call` 真实 curl 复验 `calc` + `expr` 返回 4/42；新增 Claude Code/Messages 与 Codex/Responses 工具编排回归，证明模型返回 `calc`/`expr` 时会执行真实工具并回填结果。

39. **Admin 模型拉取 GET query 可被误用为临时上游覆盖**
   - 问题：GET `/admin/upstream-models.json?base_url=...` 如果接受 query 覆盖，浏览器 Basic Auth 场景下可能把保存的上游 Authorization header 发到非预期 URL。
   - 修复：GET 只使用已保存 active profile；表单临时 `base_url` / `path_models` / `api_key` 覆盖仅允许 POST body，且 POST 先经过 Admin Origin/Referer 防护；新增回归测试验证 GET query 覆盖不会触达外部 sink。

40. **Codex `/v1/responses` 返回缺少 strict Responses 顶层 shape**
   - 问题：OpenAI Chat 上游转换到 Codex/Responses 下游时，可能只返回 `output`，缺少 `object=response`、`id`、`status`、`usage` 等 Codex/SDK 常用字段。
   - 修复：新增 Responses shape 归一化；Chat->Responses 和 Responses-like passthrough 都补齐 `id/object/model/output/status/usage`；新增回归测试覆盖普通 Responses、Codex `calc`/`expr` 工具链和空 output strict shape。

41. **Admin 暴露的 `max_concurrent_requests` 之前未在 HTTP 入口强制执行**
   - 问题：UI/配置/docs 都提供 `gateway.max_concurrent_requests` 与 `concurrency_queue_timeout_seconds`，但实际 API 路由没有获取 `_acquire_request_slot()`，导致下游并发阀门只是配置项。
   - 修复：HTTP `/v1/*`、direct tools、token count 和 `/v1/models` 入口统一包裹 `_request_slot_scope()`，超过上限返回结构化 429；新增回归测试直接占满槽位后请求 `/v1/tools/call`，确认不会绕过并发限制。

42. **Claude Code 本地文件读取 smoke 中弱上游只输出“我要读取”但不发工具调用**
   - 问题：当上游标记不支持 native tools、Gateway 走文本工具适配时，真实上游有时不会按 `<function=Read>` 发起工具调用，而是只回答“Let me read that file”，导致本地文件读取类 smoke exit 0 但没有读到文件内容。
   - 修复：local planner 的路径识别从仅支持 `@path` 扩展到绝对路径、相对路径和常见源码/文本文件名；对“read/show/cat/open/查看/读取”等点名文件请求，默认不在 Gateway 服务机读文件，而是直接合成下游原生工具请求（Anthropic `tool_use` / Chat `tool_calls` / Responses `function_call`），要求客户端在用户机器执行并把 tool_result 返回 Gateway。只有显式 `gateway.execute_user_side_tools_in_gateway=true` 才保留旧本地代理式执行；`delegate_tools_to_downstream=false` 不再授权云端本地执行用户 workspace 工具。

43. **服务启动目录与下游项目目录必须隔离**
   - 问题：Gateway 作为中游服务启动在自身仓库时，不能把服务 cwd 当成 Claude Code/Codex 的项目目录；`/Users/sanbo/Desktop/PersonalAIBrain/.traces` 这类路径属于下游项目级目录，Skills/plugin/Memory 也必须按下游项目根隔离。
   - 修复：请求级 workspace root 改为 `ContextVar`，并从请求显式字段、Claude Code `Primary working directory` / `Worktree`、Codex `<environment_context><cwd>`、metadata `projectDir` / `cwd` 等来源解析下游项目根；`Skill` 扫描项目 `.codex/.claude/.opencode/.agents/skills`、`skills/` 和项目内插件 manifest，拒绝插件 skills 跳出项目根；`multi_tool_use.parallel` 继承当前请求根；`Memory`/`RecallMemory` 默认只读写当前项目根；普通转发和 streaming passthrough 都会剥离 Gateway 内部 workspace/project 路由字段，包括 metadata JSON 字符串和 `metadata.user_id` 内嵌 JSON。
   - 验证：新增可复跑脚本 `tests/integration/project_scope_cli_smoke.py`；live smoke `.gateway_runtime/project-scope-cli-smoke-20260525-035342/summary.json` 为 `pass=true`，覆盖 direct Skills、plugin skill、`/v1/functions/call`、相对/绝对 `.traces`、Memory 项目根隔离、Anthropic SSE、Responses SSE、Claude Code CLI 和 Codex CLI。`./scripts/mimo_gateway.sh verify` 已纳入该 smoke，`GATEWAY_VERIFY_REQUIRE_CLI=1` 会强制 Claude/Codex CLI 必须通过。

## 4. 当前验证结果

```bash
python3 -m compileall -q src tests
# OK

bash -n scripts/mimo_gateway.sh scripts/deploy.sh scripts/generate-ssl.sh scripts/claude_m1.sh scripts/install_deps.sh
# OK

python3 -m pytest -q
# 886 passed, 2 skipped

GATEWAY_VERIFY_MODEL_REQUESTS=0 GATEWAY_VERIFY_DIRECT_REQUESTS=24 GATEWAY_VERIFY_WORKERS=8 GATEWAY_VERIFY_REQUIRE_CLI=1 ./scripts/mimo_gateway.sh verify
# unittest tests OK; tool acceptance OK; security/auth guardrails OK; 24-request concurrency/performance smoke OK; Claude/Codex project-scope smoke OK

# HTTP/UI/API smoke on local 127.0.0.1:8885
# GET /healthz, /ui, /admin/upstream-models.json
# POST /v1/chat/completions, /anthropic/v1/messages, /v1/tools/call, /v1/functions/call
# Codex /v1/responses and Claude /v1/messages calc/expr tool-chain regressions
# Codex /v1/responses strict object=response shape
# OK
# artifact: .gateway_runtime/final-smoke-20260524-074454-goal-audit/summary.json
# reusable project-scope smoke: tests/integration/project_scope_cli_smoke.py --require-claude --require-codex
# example project-scope smoke artifact: .gateway_runtime/project-scope-cli-smoke-20260525-035342/summary.json

# Claude Code local-file smoke
# ANTHROPIC_BASE_URL=http://127.0.0.1:8885/anthropic ANTHROPIC_AUTH_TOKEN=<gateway-key> claude -p "Read local file <probe> and answer with only the value after gateway-local-file-probe."
# exit 0; stdout: 2+2=4; no too-long / malformed / empty response marker
# artifact: .gateway_runtime/claude-local-file-probe-20260524-074546-goal-audit.summary.json
```

## 5. 仍建议后续处理

1. **提交 GitHub 前安全闸口**
   - 已清理远程 URL 中的嵌入式 token，并通过 `.gitignore` / `.dockerignore` 排除本地配置、SQLite、runtime、trace、workspace、SSL 私钥等运行数据。提交前仍需以 `git ls-files --cached --others --exclude-standard` 为候选集做 secret scan。

2. **增加真实 provider smoke**
   - unittest 已覆盖 fake upstream 和核心逻辑；真实三方 provider 的 native-tools/weak-tools 差异仍建议用带凭据的集成脚本周期性验证。

3. **继续减少历史文档漂移**
   - README 和核心 docs 已按当前实现同步；更早的专题分析文档仍应在后续改功能时顺手核对。
