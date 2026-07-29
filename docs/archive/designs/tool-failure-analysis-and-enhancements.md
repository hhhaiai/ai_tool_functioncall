# Tool failure analysis and compatibility enhancements

Date: 2026-05-16

## Source of truth

- Runtime request/failure database: `gateway_log.sqlite3`.
- Legacy imported failures: `.gateway_tool_failures.jsonl`.
- Current real runtime implementation: split `src/gateway_*` modules, especially `gateway_tool_runtime.py`, `gateway_builtin_tools.py`, `gateway_http_handler.py`, and `gateway_context.py`.
- Verification entrypoint: `tests/integration/smoke_gateway_tools.py`.

## Observed request shape

SQLite request counters at the time of analysis showed the gateway is mostly exercised through direct tool calls and Claude-compatible API paths:

| Path | Count |
| --- | ---: |
| `/v1/tools/call` | 100 |
| `/v1/chat/completions` | 18 |
| `/v1/messages/count_tokens` | 12 |
| `/v1/messages` | 11 |
| `/v1/models` | 10 |

This supports the current priority: make the local tool runtime stable first, then layer model orchestration on top.

## Failure classes and compatibility decisions

| Tool | Failure type | Evidence | Compatibility/enhancement |
| --- | --- | --- | --- |
| `Task` | `connector_required` | legacy JSONL + SQLite imported rows | Now mapped to real `Agent` alias. It calls upstream as a subtask and supports chunk/fanout for large prompts. |
| `code_interpreter` | `connector_required` | older imported rows | Now implemented as local Python execution, gated by `GATEWAY_ALLOW_SHELL_TOOLS=1`. |
| `code_interpreter` | `permission_denied` | current tests with default config disabled shell | Expected secure default. `scripts/mimo_gateway.sh` enables shell for Claude Code local gateway. Added markdown fenced-code extraction from `description`. |
| `Read` | `not_found` for `<![CDATA[src/gateway_app.py]]>` | legacy row | Path/string normalizer strips CDATA/XML-ish wrappers before path resolution. |
| `not_installed_tool` | `tool_not_found` | test-generated rows | Expected diagnostic path; retained for unsupported tool observability. |

## Enhancements added in this pass

### Network

`WebFetch` now supports:

- `method`
- `headers`
- `body`
- `json` / `body_json`
- `form`
- response status/final URL/title prefix

`WebSearch` now supports configurable `search_url`, which makes it testable against local HTTP and still defaults to DuckDuckGo HTML.

### Vision / image inspection

`view_image` now returns richer local metadata:

- detected format
- width / height
- mode
- frame count
- average RGB
- optional histogram/extrema when `histogram=true`
- optional base64 when `max_bytes > 0`

Aliases added: `ImageInfo`, `AnalyzeImage`, `image_info`, `analyze_image`, `inspect_image`.

### Code execution

`code_interpreter` now accepts markdown fenced Python code in `description`, in addition to `code` / `input` / `script` / `source`.

### HTTP Actions

HTTP Action executor now matches the documented production contract:

- `GET` / `DELETE` arguments are encoded into query strings; mutating methods send JSON body.
- Header values can reference environment variables with `${ENV_NAME}` so credentials do not need to live in config files.
- Success and error responses are capped by `max_bytes` (default 1MB) to prevent large connector replies from bloating context or logs.
- HTTP status errors, connection failures, invalid URL schemes, and response-size violations are recorded as real tool failures.
- Actions do not retry by default, avoiding duplicate side effects for POST/PUT/PATCH services; per-action `max_retries` remains available as an explicit opt-in.

### Text intent recognition

Added `IntentDetect` tool with aliases `intent_detect`, `intent_recognition`, `TextIntent`, `text_intent`.

It classifies prompt intent and suggests real gateway tools for:

- project/code analysis
- code modification
- shell/test/build execution
- network fetch/search
- vision/image inspection
- parallel tool fanout

## Verification

Current gate:

```bash
python3 -m py_compile src/toolcall_gateway.py src/gateway_app.py src/gateway_builtin_tools.py tests/test_gateway.py tests/integration/smoke_gateway_tools.py
python3 -m unittest discover -s tests -v
./scripts/mimo_gateway.sh
./tests/integration/smoke_gateway_tools.py
```

Latest result:

- Unit tests: latest full suite `Ran 150 tests ... OK`.
- Smoke repeated twice: `ok=true`.
- Smoke checks cover: project tree, glob, Python symbols, write/edit/read, Bash coding, code interpreter, web fetch, web search, image/vision metadata, intent detection, parallel tools, cleanup, SQLite-only logging.
