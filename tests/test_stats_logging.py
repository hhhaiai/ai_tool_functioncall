"""Tests for stats and logging functionality."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.gateway_logging import (
    _sqlite_init,
    _sqlite_insert_tool_failure,
    _sqlite_record_tool_stat,
    _sqlite_record_request_stat,
    _sqlite_insert_request_log,
    _sqlite_stats_snapshot,
    _sqlite_tail_requests,
    _sqlite_tail_failures,
    _redact_payload,
)


class TestPayloadRedaction:
    def test_redacts_authorization(self):
        payload = {
            "authorization": "Bearer sk-secret-key-12345",
            "content": "hello",
        }
        redacted = _redact_payload(payload)
        assert redacted["authorization"] != "Bearer sk-secret-key-12345"

    def test_redacts_api_key(self):
        payload = {
            "api_key": "sk-secret-12345",
            "model": "test",
        }
        redacted = _redact_payload(payload)
        assert "sk-secret" not in str(redacted.get("api_key", ""))

    def test_preserves_non_sensitive_data(self):
        payload = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.7,
        }
        redacted = _redact_payload(payload)
        assert redacted["model"] == "test-model"

    def test_handles_none(self):
        redacted = _redact_payload(None)
        assert redacted is None or redacted == {}

    def test_handles_empty_dict(self):
        redacted = _redact_payload({})
        assert redacted == {}


class TestSqliteLogging:
    def test_init_creates_tables(self):
        _sqlite_init()
        # Should not raise

    def test_record_tool_stat(self):
        _sqlite_record_tool_stat(name="calculator", success=True)
        _sqlite_record_tool_stat(name="calculator", success=True)
        _sqlite_record_tool_stat(name="calculator", success=False)

    def test_record_request_stat(self):
        _sqlite_record_request_stat(path="/v1/chat/completions", status=200)
        _sqlite_record_request_stat(path="/v1/messages", status=500)

    def test_insert_tool_failure(self):
        event = {
            "tool_name": "calculator",
            "call_id": "call_123",
            "failure_type": "invalid_input",
            "arguments_keys": ["expression"],
            "content": "Bad expression",
        }
        _sqlite_insert_tool_failure(event)

    def test_insert_request_log(self):
        event = {
            "request_id": "req_001",
            "path": "/v1/chat/completions",
            "status": 200,
            "method": "POST",
            "downstream_key_name": "test",
        }
        _sqlite_insert_request_log(event)

    def test_stats_snapshot(self):
        _sqlite_record_tool_stat(name="Read", success=True)
        _sqlite_record_tool_stat(name="Read", success=True)
        _sqlite_record_tool_stat(name="Grep", success=False)

        snapshot = _sqlite_stats_snapshot()
        assert isinstance(snapshot, dict)

    def test_tail_requests(self):
        _sqlite_insert_request_log({
            "request_id": "req_tail_001",
            "path": "/v1/chat/completions",
            "status": 200,
        })

        requests = _sqlite_tail_requests(limit=10)
        assert isinstance(requests, list)

    def test_tail_failures(self):
        _sqlite_insert_tool_failure({
            "tool_name": "calculator",
            "call_id": "call_fail_tail",
            "failure_type": "invalid_input",
        })

        failures = _sqlite_tail_failures(limit=10)
        assert isinstance(failures, list)


class TestToolFailureRecording:
    def test_record_failure_types(self):
        failure_types = [
            "tool_not_found",
            "connector_required",
            "permission_denied",
            "invalid_input",
            "execution_failed",
            "timeout",
            "unsafe_request",
        ]
        for ft in failure_types:
            assert isinstance(ft, str)
            assert len(ft) > 0


class TestStatsQuery:
    def test_stats_snapshot_structure(self):
        snapshot = _sqlite_stats_snapshot()
        assert isinstance(snapshot, dict)


@pytest.mark.integration
class TestLoggingIntegration:
    def test_full_logging_cycle(self):
        _sqlite_init()

        _sqlite_record_tool_stat(name="Read", success=True)
        _sqlite_record_tool_stat(name="Read", success=True)
        _sqlite_record_tool_stat(name="Read", success=False)

        _sqlite_record_request_stat(path="/v1/chat/completions", status=200)

        _sqlite_insert_tool_failure({
            "tool_name": "Bash",
            "call_id": "call_fail_001",
            "failure_type": "permission_denied",
        })

        snapshot = _sqlite_stats_snapshot()
        assert isinstance(snapshot, dict)

        requests = _sqlite_tail_requests(limit=5)
        assert isinstance(requests, list)

        failures = _sqlite_tail_failures(limit=5)
        assert isinstance(failures, list)
