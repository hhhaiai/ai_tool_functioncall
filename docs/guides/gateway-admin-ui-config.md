# Gateway Admin UI 与 Config Center

> 最后校准：2026-08-25。管理面分为运维 Control Center `/ui`、schema-driven Config Center `/ui/config` 和下游客户端配置中心 `/client-config`。

## 入口

| 路径 | 用途 | 鉴权 |
|---|---|---|
| `/ui` | 上游、下游 key、工具、MCP、HTTP Actions、Skills、请求/失败/runtime 状态 | Admin Basic Auth |
| `/ui/config` | 10-Tab canonical 配置中心 | Admin Basic Auth |
| `/client-config` | Codex、Claude Code、OpenCode 配置片段 | Admin Basic Auth |
| `/client-config.json` | 同上，机器可读 JSON | Admin Basic Auth |
| `/api/config` | 脱敏配置和 revision；POST 兼容更新 | Admin Basic Auth |
| `/api/config/schema` | 10 Tab / 94 字段 schema | Admin Basic Auth |
| `/api/config/update` | revision-aware schema-bound 更新 | Admin Basic Auth + same-origin/CLI |
| `/api/config/model-capability` | revision-aware per-model 能力覆盖更新 | Admin Basic Auth + same-origin/CLI |
| `/api/stats/dashboard` | 主 HTTP snapshot 与辅助统计 | Admin Basic Auth |
| `/api/cache/stats` | 内存/持久缓存状态 | Admin Basic Auth |
| `/api/cache/clear` | 清空缓存 | Admin Basic Auth + same-origin/CLI |
| `/api/upstreams/status` | 上游 pool、熔断和最近结果 | Admin Basic Auth |
| `/api/intelligence/status` | Intelligence provider 状态 | Admin Basic Auth |

本地开发模板可使用 `admin/admin`；生产必须通过配置或 `GATEWAY_ADMIN_PASSWORD` 替换。下游 API key 没有可用于生产的公开默认值，应设置 `GATEWAY_DOWNSTREAM_KEY` 或配置 `downstream_keys`。

## Control Center 与 Config Center 的边界

### `/ui`

用于运维和 catalog 管理：

- active upstream/profile 与模型列表；
- downstream keys；
- capability badges；
- built-in tools、MCP tools、HTTP Actions；
- Skills catalog；
- requests、failures、memories、Agent Runtime events；
- storage 和运行统计；
- 传统 Admin mutation endpoints。

### `/ui/config`

用于 canonical 配置：

| Tab | 字段数 | 范围 |
|---|---:|---|
| upstream | 11 | URL、key、model、protocol、timeout、token 上限等 |
| capabilities | 13 | tools/function/parallel/vision/stream/json/network/search 与 image/music/video/audio/speech |
| context | 11 | compact、fan-out、memory |
| intelligence | 12 | 请求前分析、LLM provider、strict/fallback |
| concurrency | 9 | upstream pool、请求并发、准入 |
| cache | 8 | semantic/tool cache、TTL、阈值 |
| tools | 12 | tool mode、rounds、权限、adapter |
| web2api | 10 | enable、timeout、大小、网络边界 |
| security | 8 | CORS、限流、管理安全等 |
| model_matrix | 0 | 可编辑 per-model 能力矩阵（独立 CAS API） |
| **合计** | **94** |  |

页面 schema 来自 `src/gateway_web_config.py:get_config_schema()`，API 只接受 schema 中的字段。

## 读取配置

```bash
curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  "$BASE_URL/api/config" | python3 -m json.tool
```

响应包含：

- `config`：脱敏后的当前配置；
- `revision`：内容 revision；
- secret 字段显示 `***`，不返回明文。

读取 schema：

```bash
curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  "$BASE_URL/api/config/schema" | python3 -m json.tool
```

## 更新配置

先读取 revision，再提交嵌套配置：

```bash
REVISION=$(curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  "$BASE_URL/api/config" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')

curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -X POST "$BASE_URL/api/config/update" \
  -d '{
    "revision": "'"$REVISION"'",
    "config": {
      "cache": {"enabled": false, "max_entries": 222},
      "intelligence": {"use_llm": true, "strict_mode": false}
    }
  }' | python3 -m json.tool
```

更新规则：

1. revision 不匹配返回冲突，不覆盖其他管理员的新配置；
2. 未知字段、非法类型、超深嵌套和过多字段被拒绝；
3. `***` 或空 secret 不覆盖已有密钥；
4. 保存使用原子替换；
5. 修改后按影响范围刷新 cache、upstream pool、Web2API 或 Assistants store；
6. Intelligence 每次请求读取当前配置，不依赖单独 reset。

浏览器写请求必须通过 Origin/Referer same-origin 校验；CLI 不发送这些头时可以使用 Basic Auth 调用。

## 状态卡

Config Center 并行读取：

```text
/api/stats/dashboard
/api/cache/stats
/api/upstreams/status
/api/intelligence/status
```

页面显示三类状态：

- 绿色：HTTP/JSON/schema 正常，且具备当前可证明的健康条件；
- 黄色：API 可用但状态降级，例如没有成功上游调用、全部 profile 不可用、连续失败、Provider 未注册或最近失败；
- 红色：HTTP 错误、无效 JSON 或非对象 JSON。

`upstream.healthy` 只表示熔断器当前未打开；页面还要求已有成功调用且连续失败为 0，才把 profile 计入已验证可用。

## 上游模型列表

使用已保存 active profile：

```bash
curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  "$BASE_URL/admin/upstream-models.json" | python3 -m json.tool
```

使用表单中的临时配置时必须 POST，避免把临时 API key 放入 query：

```bash
curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -X POST "$BASE_URL/admin/upstream-models.json" \
  --data-urlencode 'base_url=https://provider.example' \
  --data-urlencode 'protocol=openai_chat' \
  --data-urlencode 'path_models=/v1/models'
```

## MCP 与 HTTP Actions

Control Center 可以管理：

- MCP server 配置和 tools/list；
- MCP public name `mcp__server__tool` / compatibility alias；
- HTTP Action 名称、method、URL、headers、schema、timeout、大小和私网策略。

HTTP Actions 的执行边界：

- URL 只能是绝对 `http(s)`；
- 默认拒绝 localhost、私网、link-local、multicast、危险 DNS 解析和 redirect；
- 单 action 只有管理员显式配置 `allow_private_network=true` 才能访问内部地址；
- headers 可引用 `${ENV_NAME}`，避免把 token 写入仓库；
- response body 有 `max_bytes` 上限；
- HTTP 4xx/5xx、连接失败、非法 URL、timeout 和超限都作为真实工具失败；
- 默认不重试有副作用请求，只有 action 显式设置 `max_retries` 才重试。

## 下游客户端配置

`/client-config` 和 `/client-config.json` 只生成可复制片段，不写用户主目录。当前输出包括：

- Codex `config.toml` / `auth.json`；
- OpenCode provider JSON；
- Claude Code 环境变量/函数；
- VSCode Claude Code settings；
- Gateway base URL、downstream key、model alias、context/output 上限。

`gateway.client_snippet_api_key` 会同步为可认证的 downstream key，避免复制出的配置不可用。

## 安全和持久化

- 配置文件通常为 ignored `.gateway_service.json`；
- `admin.password` 保存时转换为 hash；
- 配置展示、日志和状态递归遮盖 token、secret、password、cookie、API key 和 key hash；
- 配置文件损坏或根节点不是对象时 fail closed，不回退默认无鉴权状态；
- `.gateway_runtime/`、`gateway_log.sqlite3` 和本地配置不是清理脚本可随意删除的缓存；
- 生产必须使用非默认 Admin 密码、有效 downstream key、TLS 和受控监听地址。

## 浏览器回归

Config Center 有真实 Playwright/Chromium 回归，验证：

- 四个状态请求；
- 正常、降级、HTTP 错误和无效 JSON；
- 刷新按钮；
- `/client-config` 和 stats 导航；
- revision update、secret 和 same-origin 边界。

CI 设置 `GATEWAY_REQUIRE_BROWSER_TESTS=1`，缺少 Playwright 或 Chromium 会失败，不会静默 skip。

```bash
python3 -m pytest -q tests/test_gateway_admin_api.py tests/test_web_config.py
./scripts/ci_gate.sh
```
