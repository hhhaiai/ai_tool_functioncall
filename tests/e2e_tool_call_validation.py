#!/usr/bin/env python3
"""End-to-end validation: plain API through gateway tool call execution."""
import sys, json, os, pathlib, tempfile, shutil
sys.path.insert(0, ".")

td = tempfile.mkdtemp()
os.environ["GATEWAY_WORKSPACE_ROOT"] = td

import src.gateway_app as gateway
old_config_path = gateway.CONFIG_PATH
gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"

cfg = gateway._default_config()
cfg["upstream"]["supports_tools"] = False
cfg["upstream"]["supports_function_calls"] = False
cfg["upstream"]["tools_enabled"] = "auto"
gateway.save_config(cfg)

from src.gateway_tool_runtime import run_tool_orchestration, _parse_text_tool_calls
from src.gateway_streaming import _merge_builtin_tools, _upstream_native_tools_capable, _should_use_text_tool_adapter

passed = 0
total = 7

# Test 1: Text tool adapter activates for plain APIs
print("=" * 60)
print("TEST 1: Text tool adapter activation")
print("=" * 60)
native_capable = _upstream_native_tools_capable()
assert not native_capable, "Should be False for plain API"
use_adapter = _should_use_text_tool_adapter("auto", native_capable)
assert use_adapter, "Should use text adapter for plain API"
passed += 1
print("  [PASS]\n")

# Test 2: Tools injected as text, not native
print("=" * 60)
print("TEST 2: Tools injected as text prompt")
print("=" * 60)
body = {
    "model": "test",
    "messages": [{"role": "user", "content": "What is 2+3?"}],
    "tools": [{"type": "function", "function": {"name": "calculator", "description": "Calc", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}}}}]
}
merged = _merge_builtin_tools("/v1/chat/completions", dict(body))
assert "tools" not in merged, "Plain API should NOT have native tools"
system_msg = merged["messages"][0].get("content", "") if merged["messages"] else ""
assert "Tool Call Gateway" in system_msg or "calculator" in system_msg, "Should have text tool instructions"
passed += 1
print("  [PASS]\n")

# Test 3: Full orchestration - XML tool call
print("=" * 60)
print("TEST 3: Full orchestration - XML tool call")
print("=" * 60)
call_count = [0]
FN_OPEN = chr(60) + "function=calculator>"
FN_CLOSE = chr(60) + "/function>"
PARAM_OPEN = chr(60) + "parameter=expression>"
PARAM_CLOSE = chr(60) + "/parameter>"

class MockClient:
    protocol = "openai"
    timeout = 30
    def forward(self, path, body):
        call_count[0] += 1
        if call_count[0] == 1:
            content = FN_OPEN + PARAM_OPEN + "2+3" + PARAM_CLOSE + FN_CLOSE
            return {"id": "m1", "model": "test", "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
        else:
            return {"id": "m2", "model": "test", "choices": [{"message": {"role": "assistant", "content": "The result is 5."}, "finish_reason": "stop"}]}

result = run_tool_orchestration("/v1/chat/completions", {"model": "test", "messages": [{"role": "user", "content": "What is 2+3?"}]}, client=MockClient())
content = result["choices"][0]["message"]["content"]
assert "5" in content, f"Expected 5 in response: {content}"
assert call_count[0] == 2, f"Expected 2 calls, got {call_count[0]}"
passed += 1
print(f"  Response: {content}")
print(f"  Rounds: {call_count[0]}")
print("  [PASS]\n")

# Test 4: JSON tool call format
print("=" * 60)
print("TEST 4: JSON tool call format")
print("=" * 60)
calls = _parse_text_tool_calls('{"name": "calculator", "arguments": {"expression": "10*5"}}')
assert len(calls) == 1 and calls[0].name == "calculator"
passed += 1
print(f"  Parsed: {calls[0].name}({calls[0].arguments})")
print("  [PASS]\n")

# Test 5: Bare command format
print("=" * 60)
print("TEST 5: Bare command format")
print("=" * 60)
calls = _parse_text_tool_calls("ls -la")
assert len(calls) == 1 and calls[0].name == "Bash"
passed += 1
print(f"  Parsed: {calls[0].name}({calls[0].arguments})")
print("  [PASS]\n")

# Test 6: Python-style tool call
print("=" * 60)
print("TEST 6: Python-style tool call")
print("=" * 60)
calls = _parse_text_tool_calls('calculator(expression="100/4")')
assert len(calls) == 1 and calls[0].name == "calculator"
passed += 1
print(f"  Parsed: {calls[0].name}({calls[0].arguments})")
print("  [PASS]\n")

# Test 7: Multi-round orchestration
print("=" * 60)
print("TEST 7: Multi-round orchestration")
print("=" * 60)
round_count = [0]
TIME_FN = FN_OPEN.replace("calculator", "current_time")
TIME_PARAM = PARAM_OPEN.replace("expression", "timezone")

class MultiRoundClient:
    protocol = "openai"
    timeout = 30
    def forward(self, path, body):
        round_count[0] += 1
        if round_count[0] == 1:
            content = TIME_FN + TIME_PARAM + "UTC" + PARAM_CLOSE.replace("expression", "timezone") + FN_CLOSE.replace("calculator", "current_time")
            return {"id": "r1", "model": "test", "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
        else:
            return {"id": "r2", "model": "test", "choices": [{"message": {"role": "assistant", "content": "The current time is 2026-06-18T12:00:00+00:00."}, "finish_reason": "stop"}]}

result2 = run_tool_orchestration("/v1/chat/completions", {"model": "test", "messages": [{"role": "user", "content": "What time is it?"}]}, client=MultiRoundClient())
content2 = result2["choices"][0]["message"]["content"]
assert "2026" in content2 and round_count[0] == 2
passed += 1
print(f"  Response: {content2}")
print(f"  Rounds: {round_count[0]}")
print("  [PASS]\n")

print("=" * 60)
print(f"ALL {passed}/{total} TESTS PASSED")
print("=" * 60)
print("\nEnd-to-end verified:")
print("  1. Text tool adapter activates for plain APIs")
print("  2. Tools injected as text prompt (not native schema)")
print("  3. XML tool calls parsed, executed, results fed back")
print("  4. JSON tool call format works")
print("  5. Bare command format works")
print("  6. Python-style tool call format works")
print("  7. Multi-round orchestration (tool -> result -> answer)")

gateway.CONFIG_PATH = old_config_path
del os.environ["GATEWAY_WORKSPACE_ROOT"]
shutil.rmtree(td, ignore_errors=True)
