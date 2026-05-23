# Tool Format Compatibility Analysis

**Project:** ai_tool_functioncall gateway
**Last Updated:** 2026-05-23
**Status:** Core conversions and runtime lifecycle verified in the split-module gateway
**Test Suite:** 148 unittest cases passing

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

### 3.2 Provider: 47.85 (47.85.40.209:8885) — Tool Calls DO NOT Trigger

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

| Factor | fufu | 47.85 |
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

### 4.1 Orchestrate vs Passthrough

The gateway operates in two modes:

**Passthrough mode** (primary):
- Client sends request → Gateway translates format → Upstream processes → Gateway translates response → Client receives
- Used when upstream provider natively supports tool calls
- Minimal latency overhead, preserves provider capabilities

**Orchestrate mode** (fallback):
- Gateway acts as tool runtime when upstream doesn't support native tools
- Gateway strips tools from request, sends plain prompt to upstream
- Parses tool calls from response text (`_parse_text_tool_calls`)
- Executes tools, injects results, loops until final answer
- Higher latency, but works with any upstream

**Decision rationale:** Passthrough is preferred because:
1. Preserves native provider tool runtime (streaming, parallel calls, etc.)
2. Lower latency (no gateway-side execution loop)
3. Provider may have optimizations (e.g., tool_choice forcing, structured output)

### 4.2 Gateway as Tool Runtime

When the gateway IS the tool runtime (orchestrate mode):

```
Client Request
  ↓
Gateway: detect tools in request
  ↓
Gateway: probe upstream capability
  ↓
┌─ Native tools supported → Passthrough
└─ No native tools → Orchestrate
     ↓
   Strip tools from request
     ↓
   Send modified prompt to upstream
     ↓
   Parse response for tool calls (structured or text)
     ↓
   Execute tools (MCP / HTTP / builtin)
     ↓
   Inject results into conversation
     ↓
   Loop until no more tool calls
     ↓
   Return final answer to client
```

### 4.3 Capability Probe Strategy

Current probe sends a simple request with tools and checks if the response contains tool_calls. The audit report identifies this as insufficient:

**Current approach:** `tool_choice: "auto"` → check for tool_calls in response
**Problem:** Auto means the model decides; a simple query like "2+2" may not trigger tool use
**Recommended:** Use forced `tool_choice: {type: "function", function: {name: "echo_probe"}}` to verify provider actually supports tool forcing

**Capability levels:**
- `native_tools_full` — Forced tool_choice works, roundtrip works, streaming works
- `native_tools_partial` — Auto works but forcing unreliable
- `native_tools_none` — No structured tool support, need text parsing fallback

---

## 5. Current Reliability Status and Next Steps

### 5.1 已落地的生产化能力

| Capability | Current status | Evidence |
|---|---|---|
| Forced tool-choice probe shape | Implemented for Chat / Responses / Anthropic | `_probe_body()` tests verify forced `echo_probe` shapes |
| Tool roundtrip orchestration | Implemented | Chat / Responses / Messages orchestration tests execute local tools and append results |
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
Total tests:    148 unittest cases
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

- `case.txt` — Real curl commands and responses from fufu and 47.85 providers
- `tool_gateway_audit_report.md` — Comprehensive audit of architecture and gaps
- Test suite — 148 automated unittest cases covering protocol conversion, streaming, orchestration, auth, request-body limits, context, MCP, HTTP Actions, sandboxing, provider failure semantics, and tracing
