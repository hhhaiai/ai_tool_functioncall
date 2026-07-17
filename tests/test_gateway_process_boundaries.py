from __future__ import annotations

import os
import pathlib
import sys
import threading
import time

import pytest

from src.gateway_errors import ToolExecutionError
from src.gateway_mcp import MCP_SESSIONS, MCP_SESSIONS_LOCK, McpSession, _mcp_get_session, _mcp_session_key
from src.gateway_process_ops import ProcessCancelledError, run_bounded_process


MCP_LOOP = r'''
import json, sys

def read_msg():
    header = b""
    while b"\r\n\r\n" not in header:
        one = sys.stdin.buffer.read(1)
        if not one:
            return None
        header += one
    length = 0
    for line in header.decode().splitlines():
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    return json.loads(sys.stdin.buffer.read(length).decode())

def write_msg(msg):
    raw = json.dumps(msg).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    sys.stdout.buffer.flush()

while True:
    msg = read_msg()
    if msg is None:
        break
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"test","version":"1"}}})
    else:
        write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"ok":True}})
'''


def _write_script(root: pathlib.Path, name: str, source: str) -> pathlib.Path:
    path = root / name
    path.write_text(source, encoding="utf-8")
    return path


def _server(script: pathlib.Path, **overrides):
    return {
        "name": script.stem,
        "command": sys.executable,
        "args": [str(script)],
        "cwd": str(script.parent),
        "timeout": 5,
        **overrides,
    }


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_shared_process_runner_bounds_multi_megabyte_stdout_and_stderr():
    result = run_bounded_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('A'*2000000); sys.stderr.write('B'*2000000)",
        ],
        timeout=10,
        stdout_limit=4096,
        stderr_limit=3072,
    )

    assert result.returncode == 0
    assert result.stdout_total_bytes == 2_000_000
    assert result.stderr_total_bytes == 2_000_000
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert "truncated" in result.stdout
    assert "truncated" in result.stderr
    assert len(result.stdout) < 5000
    assert len(result.stderr) < 4000


def test_mcp_stderr_flood_is_drained_and_bounded(tmp_path):
    script = _write_script(
        tmp_path,
        "stderr_flood_mcp.py",
        "import sys\nsys.stderr.write('E'*2000000)\nsys.stderr.flush()\n" + MCP_LOOP,
    )
    session = McpSession(_server(script, max_stderr_bytes=4096))
    try:
        assert session.request("tools/list") == {"ok": True}
        deadline = time.time() + 2
        while session.stderr_capture.total < 2_000_000 and time.time() < deadline:
            time.sleep(0.01)
        assert session.stderr_capture.total == 2_000_000
        assert session.stderr_capture.truncated is True
        assert "truncated" in session.stderr_text()
        assert len(session.stderr_text()) < 5000
    finally:
        session.close()


def test_mcp_oversized_response_closes_desynchronized_session(monkeypatch, tmp_path):
    source = MCP_LOOP.replace(
        'write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"ok":True}})',
        'sys.stdout.buffer.write(b"Content-Length: 5000\\r\\n\\r\\n"); sys.stdout.buffer.flush()',
    )
    script = _write_script(tmp_path, "oversized_mcp.py", source)
    monkeypatch.setattr("src.gateway_mcp._mcp_message_limit", lambda server=None: 1024)
    session = McpSession(_server(script))

    with pytest.raises(ToolExecutionError, match="exceeds message limit"):
        session.request("tools/list")

    assert session.proc.poll() is not None


def test_mcp_oversized_outbound_request_is_rejected_and_session_closed(monkeypatch, tmp_path):
    script = _write_script(tmp_path, "outbound_limit_mcp.py", MCP_LOOP)
    monkeypatch.setattr("src.gateway_mcp._mcp_message_limit", lambda server=None: 512)
    session = McpSession(_server(script))

    with pytest.raises(ToolExecutionError, match="request exceeds message limit"):
        session.request("tools/call", {"payload": "X" * 2000})

    assert session.proc.poll() is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_mcp_close_terminates_descendant_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    prefix = (
        "import pathlib, subprocess, sys\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
    )
    script = _write_script(tmp_path, "descendant_mcp.py", prefix + MCP_LOOP)
    session = McpSession(_server(script))
    child_pid = int(pid_file.read_text())
    assert _pid_exists(child_pid)

    session.close()
    deadline = time.time() + 3
    while _pid_exists(child_pid) and time.time() < deadline:
        time.sleep(0.02)

    assert not _pid_exists(child_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_explicit_cancellation_terminates_descendant_process_group(tmp_path):
    pid_file = tmp_path / "cancel-child.pid"
    script = _write_script(
        tmp_path,
        "cancel_tree.py",
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n",
    )
    cancelled = threading.Event()

    def cancel_when_ready():
        deadline = time.time() + 3
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.01)
        cancelled.set()

    thread = threading.Thread(target=cancel_when_ready, daemon=True)
    thread.start()
    with pytest.raises(ProcessCancelledError) as exc_info:
        run_bounded_process(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=10,
            cancel_event=cancelled,
        )
    thread.join(timeout=2)

    assert "ready" in exc_info.value.output
    child_pid = int(pid_file.read_text())
    deadline = time.time() + 3
    while _pid_exists(child_pid) and time.time() < deadline:
        time.sleep(0.02)
    assert not _pid_exists(child_pid)


def test_mcp_worker_crash_is_not_reused(tmp_path):
    source = MCP_LOOP.replace(
        'write_msg({"jsonrpc":"2.0","id":msg["id"],"result":{"ok":True}})',
        "raise SystemExit(23)",
    )
    script = _write_script(tmp_path, "crashing_mcp.py", source)
    server = _server(script, name="crashing-worker-no-reuse")
    key = _mcp_session_key(server)
    with MCP_SESSIONS_LOCK:
        old = MCP_SESSIONS.pop(key, None)
    if old:
        old.close()

    first = _mcp_get_session(server)
    first_pid = first.proc.pid
    try:
        with pytest.raises(ToolExecutionError):
            first.request("tools/list")
        assert first.proc.poll() is not None

        second = _mcp_get_session(server)
        try:
            assert second.proc.pid != first_pid
            assert second.proc.poll() is None
        finally:
            second.close()
    finally:
        with MCP_SESSIONS_LOCK:
            session = MCP_SESSIONS.pop(key, None)
        if session and session is not first:
            session.close()
