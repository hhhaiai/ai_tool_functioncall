# Agent Planner live stability fix — 2026-06-27

## Problem observed

A real client request sequence showed that the Gateway was not stable enough for the remote Agent Planner contract:

1. A short user turn (`jo`) plus client-injected tool/log context polluted the apparent session state.
2. A later `分析这套项目` request could receive a chat-only upstream non-answer such as `Hello, I can't answer this question for now...` instead of a planner-owned tool dispatch or evidence synthesis.
3. The previous full acceptance script had a false-positive risk: `env GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0 python3 -m pytest ...` used `/Users/sanbo/.local/bin/env`, which silently returned success without running/printing pytest output.
4. No-declared-tools project-analysis fallback returned `LS/Glob` tool_use blocks, but lacked enough `gateway_context.agent_planner` / audit metadata for strict scoped audit to count the turn as covered.

## Fixes

### 1. Robust final response text extraction

`src/gateway_agent_planner.py::_planner_response_text()` now extracts text across all supported response shapes instead of using path-exclusive branches:

- Anthropic `content`
- OpenAI Responses `output[]` and `output_text`
- OpenAI Chat `choices[].message.content`
- direct `choice.text` fallback

This prevents valid upstream synthesis from being mistaken as empty and replaced by the deterministic fallback.

### 2. Acceptance gate no longer uses PATH `env`

`scripts/agent_planner_acceptance.sh --full` now runs full pytest via a subshell export:

```bash
(
  export GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=0
  python3 -m pytest -ra tests
)
```

This avoids the local `/Users/sanbo/.local/bin/env` wrapper and makes the full gate print the real pytest summary.

### 3. Compatibility default separated from remote strict config

`src/gateway_config.py::_default_config()` now defaults `gateway.agent_planner_strict_every_turn` to false when no environment/config value is provided.

Remote service strict mode is still enabled by explicit runtime config (`gateway.config.json` / `.gateway_service.json`), while legacy/unit compatibility tests can run without being forced into remote strict behavior.

### 4. No-tools project fallback is still planner-visible

For `分析这套项目` requests without explicit tools, the runtime keeps the bounded fallback behavior: it surfaces downstream client tool requests (`LS`, `Glob`, `Glob`) instead of sending the upstream a fake file-analysis request.

The response now includes planner context:

```json
{
  "strategy": "gateway_downstream_tool_request",
  "agent_planner": {
    "workflow": "project_analysis",
    "step": "project_structure",
    "intent": {"kind": "project_analysis"}
  }
}
```

The tool-dispatch runtime event also carries:

```json
{
  "owner": "downstream_client",
  "dispatch": "downstream_client"
}
```

So scoped audit counts the turn as a strict planner-owned boundary.

## Verification evidence

### Full acceptance

Command:

```bash
./scripts/agent_planner_acceptance.sh --full
```

Result:

```text
Agent Planner acceptance gate: PASS
988 passed, 2 skipped, 21 warnings in 51.97s
```

### Live service restart and smoke

Command path:

```bash
./scripts/mimo_gateway.sh restart
POST http://127.0.0.1:8885/v1/messages
```

Request intent: `分析这套项目`

Observed response summary:

```json
{
  "stop_reason": "tool_use",
  "tool_names": ["LS", "Glob", "Glob"],
  "strategy": "gateway_downstream_tool_request",
  "planner_workflow": "project_analysis",
  "planner_step": "project_structure",
  "intent_kind": "project_analysis"
}
```

Scoped audit:

```json
{
  "strict_every_turn_planner_envelope": "proven/current_scope",
  "admin_observability": "proven/current_scope",
  "covered_session_count": 1,
  "dispatch_session_count": 1,
  "missing_session_count": 0
}
```

## Operational note

The upstream model remains a chat-only synthesizer. Tool authority is owned by the Gateway Agent Planner and dispatched to the downstream client workspace. The service machine must not execute user-machine filesystem/shell tools by default.
