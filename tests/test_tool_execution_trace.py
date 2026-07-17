"""
Test tool execution trace fields: execution_ms, retry_count, provider.
Covers: _execute_tool_call, _record_tool_failure, _sqlite_insert_tool_failure.
"""

import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

import src.toolcall_gateway as gateway
from src.toolcall_gateway import ToolCall, ToolResult, ToolExecutionError


class TestToolFailureTraceFields(unittest.TestCase):
    """Verify tool_failures records contain execution_ms, retry_count, provider."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        p = pathlib.Path(self._td) / "trace.sqlite3"
        self._old_env = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
        os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(p)
        gateway.SQLITE_READY = False
        gateway._sqlite_log_conn = None
        gateway._sqlite_init()
        self._path = str(p)

    def tearDown(self):
        gateway._sqlite_log_conn = None
        if self._old_env is None:
            os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
        else:
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = self._old_env

    def _rows(self):
        conn = sqlite3.connect(self._path)
        cur = conn.execute(
            "SELECT tool_name, failure_type, execution_ms, retry_count, provider FROM tool_failures ORDER BY ts"
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def test_failure_records_execution_ms(self):
        call = ToolCall(call_id="t1", name="bad_tool", arguments={}, raw={})
        result = ToolResult(
            call_id="t1", name="bad_tool",
            content="ToolNotFound", success=False, failure_type="tool_not_found",
        )
        gateway._record_tool_failure(call, result, execution_ms=42.5, retry_count=0, provider="openai")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        name, ftype, exec_ms, retry_cnt, prov = rows[0]
        self.assertEqual(exec_ms, 42.5)
        self.assertEqual(retry_cnt, 0)
        self.assertEqual(prov, "openai")

    def test_failure_records_retry_count(self):
        call = ToolCall(call_id="t2", name="bad_tool", arguments={}, raw={})
        result = ToolResult(
            call_id="t2", name="bad_tool",
            content="fail", success=False, failure_type="execution_failed",
        )
        gateway._record_tool_failure(call, result, execution_ms=10.0, retry_count=3, provider="anthropic")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        name, ftype, exec_ms, retry_cnt, prov = rows[0]
        self.assertEqual(retry_cnt, 3)
        self.assertEqual(prov, "anthropic")

    def test_failure_records_provider(self):
        for prov in ("openai", "anthropic", "direct", "unknown"):
            call = ToolCall(call_id=f"t_{prov}", name="bad_tool", arguments={}, raw={})
            result = ToolResult(
                call_id=f"t_{prov}", name="bad_tool",
                content="fail", success=False, failure_type="tool_not_found",
            )
            gateway._record_tool_failure(call, result, execution_ms=1.0, retry_count=0, provider=prov)
        rows = self._rows()
        self.assertEqual(len(rows), 4)
        providers = {r[4] for r in rows}
        self.assertEqual(providers, {"openai", "anthropic", "direct", "unknown"})

    def test_failure_content_is_redacted_and_bounded(self):
        saved_config = gateway._gateway_config
        cfg_val = gateway._gateway_config() if callable(gateway._gateway_config) else dict(gateway._gateway_config)
        cfg_val["max_log_payload_chars"] = 220
        gateway._gateway_config = (lambda: cfg_val) if callable(saved_config) else cfg_val
        try:
            secret = "Bearer " + "live-token-value"
            large_text = "x" * 1000
            gateway._record_tool_failure(
                tool_name="bad_tool",
                call_id="t_secret",
                failure_type="execution_failed",
                arguments_keys=["Authorization", "safe"],
                content=f"HTTP 500 Authorization: {secret} {large_text}",
                execution_ms=1.0,
                retry_count=0,
                provider="openai",
            )
            conn = sqlite3.connect(self._path)
            row = conn.execute("SELECT arguments_keys_json, content FROM tool_failures").fetchone()
            conn.close()
            keys = json.loads(row[0])
            content = row[1]
            self.assertEqual(keys, ["Authorization", "safe"])
            self.assertLessEqual(len(content), 220)
            self.assertIn("gateway_truncated", content)
            self.assertIn("Authorization: Bearer ***", content)
            self.assertNotIn("live-token-value", content)
            self.assertNotIn(large_text, content)
        finally:
            gateway._gateway_config = saved_config


class TestExecuteToolCallTiming(unittest.TestCase):
    """Verify _execute_tool_call measures execution_ms and passes it to failure records."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        p = pathlib.Path(self._td) / "trace.sqlite3"
        self._old_env = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
        os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(p)
        gateway.SQLITE_READY = False
        gateway._sqlite_log_conn = None
        gateway._sqlite_init()
        self._path = str(p)

    def tearDown(self):
        gateway._sqlite_log_conn = None
        if self._old_env is None:
            os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
        else:
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = self._old_env

    def _rows(self):
        conn = sqlite3.connect(self._path)
        cur = conn.execute(
            "SELECT tool_name, failure_type, execution_ms, retry_count, provider FROM tool_failures ORDER BY ts"
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def test_success_no_failure_record(self):
        """Successful builtin tool must not produce a tool_failures row."""
        # Use the exact name as registered in BUILTIN_TOOLS.
        call = gateway.ToolCall(call_id="t_ok", name="echo_probe", arguments={"value": "hello"}, raw={})
        result = gateway._execute_tool_call(call, provider="test")
        self.assertTrue(result.success)
        rows = self._rows()
        self.assertEqual(len(rows), 0)

    def test_failure_records_timing_fields(self):
        """Builtin tool that raises ToolExecutionError records execution_ms + retry_count + provider."""

        def bad_handler(args):
            raise ToolExecutionError("intentional error", failure_type="execution_failed")

        orig = gateway.BUILTIN_TOOLS.get("echo")
        gateway.BUILTIN_TOOLS["_test_trace"] = gateway.GatewayTool(
            name="_test_trace",
            description="",
            parameters={},
            handler=bad_handler,
            risk="medium",
        )
        try:
            call = ToolCall(call_id="t_bad", name="_test_trace", arguments={}, raw={})
            result = gateway._execute_tool_call(call, provider="openai")
            self.assertFalse(result.success)
            rows = self._rows()
            self.assertEqual(len(rows), 1)
            name, ftype, exec_ms, retry_cnt, prov = rows[0]
            self.assertEqual(name, "_test_trace")
            self.assertEqual(ftype, "execution_failed")
            self.assertIsNotNone(exec_ms)
            self.assertGreaterEqual(exec_ms, 0.0)
            self.assertEqual(retry_cnt, 0)
            self.assertEqual(prov, "openai")
        finally:
            gateway.BUILTIN_TOOLS.pop("_test_trace", None)
            if orig:
                gateway.BUILTIN_TOOLS["echo"] = orig

    def test_unexpected_exception_records_failure_telemetry(self):
        def broken_handler(args):
            raise RuntimeError("unexpected")

        gateway.BUILTIN_TOOLS["_test_unexpected"] = gateway.GatewayTool(
            name="_test_unexpected",
            description="",
            parameters={},
            handler=broken_handler,
            risk="medium",
        )
        try:
            call = ToolCall(call_id="t_unexpected", name="_test_unexpected", arguments={}, raw={})
            result = gateway._execute_tool_call(call, provider="direct")
            self.assertFalse(result.success)
            rows = self._rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "_test_unexpected")
            self.assertEqual(rows[0][1], "execution_failed")
            self.assertEqual(rows[0][3], 0)
            self.assertEqual(rows[0][4], "direct")
        finally:
            gateway.BUILTIN_TOOLS.pop("_test_unexpected", None)


class TestExecuteToolCallRetry(unittest.TestCase):
    """Verify _execute_tool_call retries on transient failure and records retry_count."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        p = pathlib.Path(self._td) / "trace.sqlite3"
        self._old_env = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
        os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(p)
        gateway.SQLITE_READY = False
        gateway._sqlite_log_conn = None
        gateway._sqlite_init()
        self._path = str(p)

    def tearDown(self):
        gateway._sqlite_log_conn = None
        if self._old_env is None:
            os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
        else:
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = self._old_env

    def _rows(self):
        conn = sqlite3.connect(self._path)
        cur = conn.execute(
            "SELECT tool_name, failure_type, execution_ms, retry_count, provider FROM tool_failures ORDER BY ts"
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def test_retry_exhausted_then_success(self):
        """After transient failures, tool succeeds → no failure row, retry_count not exposed."""
        attempts = {"count": 0}

        def flaky_handler(args):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ToolExecutionError(
                    "transient",
                    failure_type="execution_failed",
                    retryable=True,
                )
            return "ok"

        gateway.BUILTIN_TOOLS["_test_flaky"] = gateway.GatewayTool(
            name="_test_flaky",
            description="",
            parameters={},
            handler=flaky_handler,
            risk="medium",
        )
        # Patch _gateway_config (whether dict or callable) to return tool_max_retries=2.
        cfg_val = gateway._gateway_config() if callable(gateway._gateway_config) else dict(gateway._gateway_config)
        cfg_val["tool_max_retries"] = 2
        if callable(gateway._gateway_config):
            patch_fn = lambda: cfg_val
        else:
            patch_fn = cfg_val
        saved = gateway._gateway_config
        gateway._gateway_config = patch_fn
        try:
            call = ToolCall(call_id="t_flaky", name="_test_flaky", arguments={}, raw={})
            result = gateway._execute_tool_call(call, provider="anthropic")
            self.assertTrue(result.success)
            self.assertEqual(attempts["count"], 3)
            rows = self._rows()
            self.assertEqual(len(rows), 0)  # success → no failure record
        finally:
            gateway.BUILTIN_TOOLS.pop("_test_flaky", None)
            gateway._gateway_config = saved

    def test_non_retryable_failure_runs_once(self):
        """Permanent failures do not repeat potentially non-idempotent tools."""
        attempts = {"count": 0}

        def always_fail(args):
            attempts["count"] += 1
            raise ToolExecutionError("permanent", failure_type="execution_failed")

        gateway.BUILTIN_TOOLS["_test_perm"] = gateway.GatewayTool(
            name="_test_perm",
            description="",
            parameters={},
            handler=always_fail,
            risk="medium",
        )
        try:
            call = ToolCall(call_id="t_perm", name="_test_perm", arguments={}, raw={})
            result = gateway._execute_tool_call(call, provider="openai")
            self.assertFalse(result.success)
            self.assertEqual(attempts["count"], 1)
            rows = self._rows()
            self.assertEqual(len(rows), 1)
            name, ftype, exec_ms, retry_cnt, prov = rows[0]
            self.assertEqual(retry_cnt, 0)
            self.assertEqual(prov, "openai")
        finally:
            gateway.BUILTIN_TOOLS.pop("_test_perm", None)


class TestDirectToolCallProvider(unittest.TestCase):
    """execute_direct_tool_call must pass provider='direct'."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        p = pathlib.Path(self._td) / "trace.sqlite3"
        self._old_env = os.environ.get("GATEWAY_SQLITE_LOG_PATH")
        os.environ["GATEWAY_SQLITE_LOG_PATH"] = str(p)
        gateway.SQLITE_READY = False
        gateway._sqlite_log_conn = None
        gateway._sqlite_init()
        self._path = str(p)

    def tearDown(self):
        gateway._sqlite_log_conn = None
        if self._old_env is None:
            os.environ.pop("GATEWAY_SQLITE_LOG_PATH", None)
        else:
            os.environ["GATEWAY_SQLITE_LOG_PATH"] = self._old_env

    def test_direct_tool_call_uses_direct_provider(self):
        """Direct tool invocation must record provider='direct'."""

        def bad_handler(args):
            raise ToolExecutionError("fail", failure_type="execution_failed")

        gateway.BUILTIN_TOOLS["_test_direct"] = gateway.GatewayTool(
            name="_test_direct",
            description="",
            parameters={},
            handler=bad_handler,
            risk="medium",
        )
        try:
            body = {
                "tool_calls": [
                    {
                        "id": "tc1",
                        "function": {"name": "_test_direct", "arguments": "{}"},
                        "type": "function",
                    }
                ]
            }
            gateway.execute_direct_tool_call(body)
            conn = sqlite3.connect(self._path)
            cur = conn.execute("SELECT provider FROM tool_failures")
            rows = cur.fetchall()
            conn.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "direct")
        finally:
            gateway.BUILTIN_TOOLS.pop("_test_direct", None)


if __name__ == "__main__":
    unittest.main()
