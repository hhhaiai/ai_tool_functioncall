# Tool Format Compatibility Analysis

**Project:** ai_tool_functioncall gateway
**Last Updated:** 2026-05-23
**Status:** Core conversions and runtime lifecycle verified in the split-module gateway
**Test Suite:** 167 unittest cases passing

---

## 1. Format Matrix: 3 Protocols × 6 Conversion Paths

Legend:
- ✅ DONE — Implemented and tested
- 🔧 FIXED — Recently fixed (2025-05-18)
- ⚠️ PARTIAL — Works but needs hardening
- ❌ MISSING — Not yet implemented

| Conversion Path | OpenAI Chat | OpenAI Responses | Anthropic Messages |
|---|---|---|---|
| **Input tools** (schema generation) | ✅ DONE | ✅ DONE | ✅ DONE |
| **Output tools** (response wrapping) | ✅ DONE | ✅ DONE | ✅ DONE |
| **Tool calls extraction** | ✅ DONE | ✅ DONE | ✅ DONE |
| **Tool result injection** | ✅ DONE | ✅ DONE | ✅ DONE |
| **Streaming detection** | 🔧 FIXED | ✅ DONE | ✅ DONE |
| **Streaming emission** | 🔧 FIXED | ✅ DONE | ✅ DONE |

---

## 2. Implementation Details by Conversion Path

### 2.1 Input Tools — Schema Generation

**Function:** `_tool_schema_for_path` in `src/gateway_mcp.py` (merged by `src/gateway_streaming.py:_merge_builtin_tools`)

Generates correct tool schemas for all 3 inbound protocols:

- **OpenAI Chat:** `tools[].function.name`, `tools[].function.parameters` with `type: "function"` wrapper
- **OpenAI Responses:** `tools[].name`, `tools[].parameters` (flat, no function wrapper)
- **Anthropic Messages:** `tools[].name`, `tools[].input_schema` (no `type` field, no `tool_choice`)

Status: ✅ DONE for all 3 protocols

### 2.2 Output Tools — Response Wrapping

**Function:** `_from_openai_chat_response` in `src/gateway_protocol.py`

Normalizes upstream responses into the client's expected format. Handles:
- `finish_reason: tool_calls` detection
- `tool_calls[]` array extraction from message
- Content null handling when tool calls present

Status: ✅ DONE

### 2.3 Tool Calls Extraction

**Function:** `_extract_tool_calls` and `_parse_text_tool_calls` in `src/gateway_tool_runtime.py`

Extracts tool call information from provider responses across all formats:
- OpenAI Chat: `message.tool_calls[].function.{name, arguments}`
- OpenAI Responses: `output[]` items with `type: "function_call"`
- Anthropic: `content[]` blocks with `type: "tool_use"`

Also handles:
- **Text fallback:** `_parse_text_tool_calls` parses tool calls embedded in plain text for weak upstreams that don't return structured tool_calls
- **Arguments validation:** JSON parse with error handling

Status: ✅ DONE

### 2.4 Tool Result Injection

**Function:** `_append_tool_results` in `src/gateway_tool_runtime.py`

Injects tool execution results back into the conversation for the next turn:

- **OpenAI Chat:** Appends `role: "tool"` messages with `tool_call_id`
- **OpenAI Responses:** Appends `role: "function_call_output"` items with `call_id`
- **Anthropic:** Appends `role: "user"` messages containing `tool_result` content blocks

Status: ✅ DONE

### 2.5 Streaming Detection

**Function:** `_detect_streaming_tool_calls_from_sse` (in `gateway_streaming.py`)

Detects tool call events in SSE streams:

- **OpenAI Chat:** `choices[0].delta.tool_calls` chunks
- **OpenAI Responses:** `response.function_call_arguments.delta` events
- **Anthropic:** `content_block_start` with `type: "tool_use"` + `content_block_delta` with `input_json_delta`

**Current streaming behavior:** `gateway_streaming.py` handles SSE parsing plus downstream SSE emission. Passthrough mode proxies upstream SSE; orchestrate mode calls upstream non-streaming and emits Gateway-owned SSE after local/MCP/HTTP tool execution.

Status: ✅ DONE

### 2.6 Streaming Emission

**Function:** `_streaming_tool_event_for_path` in `src/gateway_streaming.py`

Emits correctly formatted SSE events for each protocol:

- **OpenAI Chat:** `data: {"choices":[{"delta":{"tool_calls":[...]}}]}` chunks
- **OpenAI Responses:** `event: response.function_call_arguments.delta` + `event: response.output_item.done`
- **Anthropic:** `content_block_start`, `content_block_delta`, `content_block_stop` sequence

Status: ✅ DONE

---

## 3. Real Provider Behaviors (from case.txt)

### 3.1 Provider: fufu (fufu.iqach.top) — Tool Calls WORK

**Model:** `mimo-v2.5-pro`
**Protocol:** OpenAI Chat (`/v1/chat/completions`)

```
Request:  tools=[{type:"function", function:{name:"calc", ...}}], tool_choice="auto"
Response: finish_reason="tool_calls", tool_calls=[{function:{name:"calc", arguments:'{"expr":"2+2"}'}}]
Result:   ✅ Model correctly invokes the tool
```

Key observation: The model's `reasoning_content` shows it decided to use the tool:
> "The user is asking a simple math question: what is 2+2? I can use the calculator tool to evaluate this expression."

### 3.2 Provider: provider-b (provider-b.example.local:8885) — Tool Calls DO NOT Trigger

**Model:** `mimo-v2.5-pro` (same model!)
**Protocol:** OpenAI Chat (`/v1/chat/completions`)

```
Request:  tools=[{type:"function", function:{name:"calc", ...}}], tool_choice="auto"
Response: finish_reason="stop", tool_calls=null, content="2 + 2 = **4**"
Result:   ❌ Model answers directly, ignores the tool
```

Key observation: The model's `<think>` shows it explicitly chose not to use the tool:
> "The user is asking a simple math question: 2+2=4. This is basic arithmetic that I can answer directly without needing a tool."

### 3.3 Root Cause Analysis

**Same model, different behavior.** The difference is NOT in the model but in the **provider runtime**:

| Factor | fufu | provider-b |
|---|---|---|
| Model | mimo-v2.5-pro | mimo-v2.5-pro |
| Tool schema sent | ✅ Identical | ✅ Identical |
| tool_choice | auto | auto |
| Tools triggered | ✅ Yes | ❌ No |
| Likely cause | Provider runtime properly injects tool context | Provider may strip/not forward tool definitions to model |

This proves the audit report's core thesis: **protocol compatibility ≠ tool runtime capability**.

### 3.4 Protocol Differences Observed

**OpenAI Chat vs Responses vs Anthropic (all on fufu):**

| Aspect | OpenAI Chat | OpenAI Responses | Anthropic |
|---|---|---|---|
| Tool schema field | `tools[].function` (nested) | `tools[]` (flat) | `tools[].input_schema` |
| `tool_choice` | ✅ Supported | ❌ Not available (auto only) | ❌ Not available |
| Tool call return | `message.tool_calls[]` | `output[]` function_call items | `content[]` tool_use blocks |
| Streaming events | `delta.tool_calls` | `response.function_call_arguments.*` | `content_block_*` |
| Stop reason | `finish_reason: "tool_calls"` | `status: "completed"` | `stop_reason: "tool_use"` |

---

## 4. Architecture Decisions

### 4.1 Two Independent Control Axes

The gateway uses two independent configuration axes:

**`tool_mode`** — controls the Gateway's role:

| Mode | Behavior |
|------|----------|
| `orchestrate` (default) | Gateway parses tool calls from upstream, executes gateway-owned tools locally, and surfaces user-side tools to downstream clients for execution |
| `native_passthrough` / `proxy` | Gateway passes through: upstream handles tools natively, Gateway only translates protocol formats |
| `passthrough` (legacy alias) | Same as `native_passthrough` |

**`tools_enabled`** — controls whether native tool schemas are sent to upstream:

| Value | Behavior |
|-------|----------|
| `auto` (default) | Checks `supports_tools` + `supports_function_calls` capabilities; sends native tools if both true, otherwise falls back to text tool adapter |
| `native` | Always sends native tool schemas |
| `native_only` | Sends native tools only; fails fast if capabilities are insufficient |
| `adapter` / `text_only` | Always uses Gateway's local real tool text adapter |
| `off` | No tools, no adapter |

### 4.2 Gateway as Tool Runtime (Orchestrate Mode)

When `tool_mode=orchestrate`, the Gateway is the tool runtime:

```
Client Request
  ↓
Gateway: detect tools in request
  ↓
Check tools_enabled + upstream capabilities
  ↓
┌─ tools_enabled=auto AND supports_tools=true → send native tools to upstream
├─ tools_enabled=auto AND supports_tools=false → text tool adapter (§4.4)
├─ tools_enabled=native_only AND capabilities不足 → fail-fast
└─ tools_enabled=off → no tools
     ↓
Parse response for tool calls (structured or text fallback)
     ↓
Execute tools locally (builtin / MCP / HTTP Action)
     ↓
Inject results into conversation
     ↓
Loop until no more tool calls (max rounds configurable)
     ↓
Return final answer to client
```

### 4.3 Capability Probe Strategy

The probe uses forced `tool_choice` to verify upstream tool support:

**`_probe_body(path, model)`** sends a request with `echo_probe` tool and forced `tool_choice`:
- Chat: `tool_choice: {type: "function", function: {name: "echo_probe"}}`
- Responses: `tool_choice: {type: "function", name: "echo_probe"}`
- Anthropic: `tool_choice: {type: "tool", name: "echo_probe"}`

This forces the upstream to either call the tool (proving support) or fail/ignore it (proving lack of support). Auto `tool_choice` is unreliable because the model may choose not to call tools for simple queries.

### 4.4 Text Tool Adapter with Dynamic Compaction

When the upstream doesn't support native tools, the Gateway injects text-based tool instructions and parses `<function=Tool>` / `<parameter=name>` responses.

**Dynamic compaction** prevents provider-level `too long` refusals for large Claude Code/Codex harnesses:

```
limit = max(8000, min(upstream.max_input_tokens * 0.45, config_cap))
```

| Upstream max_input_tokens | Dynamic limit | Effect |
|--------------------------|---------------|--------|
| 4k (small) | 8000 (floor) | Aggressive compaction |
| 32k | 14400 | Moderate compaction |
| 128k | 48000 (default cap) | Only compact truly large harnesses |
| 1M | 48000 (default cap) | Same; config_cap can be raised |

Config cap defaults to 48000; set to 0 to disable. Env var `GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT` overrides config.

---

## 5. Current Reliability Status and Next Steps

### 5.1 已落地的生产化能力

| Capability | Current status | Evidence |
|---|---|---|
| Forced tool-choice probe shape | Implemented for Chat / Responses / Anthropic | `_probe_body()` tests verify forced `echo_probe` shapes |
| Tool roundtrip orchestration | Implemented | Chat / Responses / Messages tests cover gateway-owned execution plus user-side tool request surfacing |
| Streaming tool event parsing | Implemented | SSE parser tests cover OpenAI Chat, Responses, Anthropic events and malformed JSON tolerance |
| Strict argument handling | Implemented with explicit failures | Invalid JSON / invalid parameters return structured tool failures |
| Tool execution tracing | Implemented | SQLite `tool_failures` records execution time, retry count, provider |
| Timeout/retry | Implemented for local shell/code tools and tool retry tracing | `GATEWAY_TOOL_EXECUTION_TIMEOUT`, shell/code timeouts, retry tests |
| Permission gating / path sandbox | Implemented | Write/shell gates default off; workspace `relative_to(root)` containment tests |
| MCP integration | Implemented baseline | `tools/list`, `tools/call`, resource and prompt helper tests |

### 5.2 仍建议继续加强

| Area | Why it still matters | Current next step |
|---|---|---|
| Real provider matrix | Providers differ even for the same model/schema | Run credentialed smoke against each commercial upstream before rollout |
| Capability registry automation | Static config can drift as providers change | Periodic probe job + Admin UI health display |
| Full downstream client E2E | Unit tests cover protocol, but clients may add edge cases | Add scheduled Codex / Claude Code / OpenCode smoke with real clients |
| Structured event replay | Helpful for production incident debugging | Persist sanitized replay bundles for failed tool loops |

## 6. Test Coverage Summary

```
Total tests:    167 unittest cases
Result:         OK
Coverage focus: protocol conversion, streaming tool events, tool orchestration, context fan-out, SQLite memory/logging, HTTP routing/auth, MCP, HTTP Actions, workspace sandbox, provider failure semantics
```

**What's tested:**
- Schema generation for all 3 protocols
- Tool call extraction from all 3 response formats
- Tool result injection for all 3 protocols
- Streaming tool call detection and emission
- Text fallback tool call parsing

**Still requires credentialed/manual coverage:**
- End-to-end tool roundtrip against each real upstream provider
- Full downstream-client E2E with Codex / Claude Code / OpenCode binaries
- Long-running malformed/slow tool behavior under production load

---

## 7. Reference Data Sources

- `case.txt` — Real curl commands and responses from fufu and provider-b providers
- `tool_gateway_audit_report.md` — Comprehensive audit of architecture and gaps
- Test suite — 167 automated unittest cases covering protocol conversion, streaming, orchestration, auth, request-body limits, bounded request/failure logging, context, MCP, HTTP Actions, sandboxing, provider failure semantics, and tracing
