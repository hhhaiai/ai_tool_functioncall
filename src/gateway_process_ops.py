#!/usr/bin/env python3
"""Shared bounded subprocess lifecycle helpers.

All Gateway-owned child processes should use the same output, timeout, and
process-tree semantics.  The retained output is bounded in memory while pipes
are drained concurrently so a noisy child cannot deadlock on a full pipe.
"""
from __future__ import annotations

import os
import pathlib
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping


class ProcessCancelledError(subprocess.SubprocessError):
    """Raised after an explicit cancellation terminates the process group."""

    def __init__(self, command: Any, *, output: str = "", stderr: str = "") -> None:
        super().__init__(f"process cancelled: {command!r}")
        self.cmd = command
        self.output = output
        self.stderr = stderr


class BoundedProcessStream:
    """Drain a byte stream while retaining a fixed-size head and tail."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self.head_limit = max(1, self.limit * 3 // 4)
        self.tail_limit = max(0, self.limit - self.head_limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    @property
    def truncated(self) -> bool:
        with self.lock:
            return self.total > self.limit

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self.lock:
            self.total += len(chunk)
            offset = 0
            if len(self.head) < self.head_limit:
                take = min(self.head_limit - len(self.head), len(chunk))
                self.head.extend(chunk[:take])
                offset = take
            if offset < len(chunk) and self.tail_limit > 0:
                self.tail.extend(chunk[offset:])
                if len(self.tail) > self.tail_limit:
                    del self.tail[:-self.tail_limit]

    def bytes(self) -> bytes:
        with self.lock:
            return bytes(self.head) + bytes(self.tail)

    def text(self) -> str:
        with self.lock:
            total = self.total
            head_bytes = bytes(self.head)
            tail_bytes = bytes(self.tail)
        if total <= self.limit:
            return (head_bytes + tail_bytes).decode("utf-8", errors="replace")
        omitted = max(0, total - len(head_bytes) - len(tail_bytes))
        head = head_bytes.decode("utf-8", errors="replace")
        tail = tail_bytes.decode("utf-8", errors="replace")
        return f"{head}\n[gateway: truncated {omitted} bytes]\n{tail}"


def process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))}
    return {"start_new_session": True}


def process_ready_pipe(
    env: Mapping[str, str] | None,
    *,
    fd_env_name: str | None,
) -> tuple[dict[str, str] | None, int | None, int | None, dict[str, Any]]:
    """Prepare an optional POSIX-only child readiness pipe.

    A policy-setting worker can inherit the write end, signal immediately
    before it replaces itself with the real command, and remove the internal
    environment variable before ``exec``.  This keeps worker setup time out of
    the caller's command timeout/read window without exposing a control marker
    in user stdout or stderr.
    """
    child_env = dict(env) if env is not None else None
    if not fd_env_name or os.name == "nt":
        return child_env, None, None, {}
    read_fd, write_fd = os.pipe()
    if child_env is None:
        child_env = dict(os.environ)
    child_env[fd_env_name] = str(write_fd)
    return child_env, read_fd, write_fd, {"pass_fds": (write_fd,)}


def wait_for_process_ready(
    proc: subprocess.Popen,
    read_fd: int | None,
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
) -> bool:
    """Wait for a child readiness byte, or return false if it exits first."""
    if read_fd is None:
        return False
    deadline = time.monotonic() + max(0.01, float(timeout))
    try:
        while True:
            if proc.poll() is not None:
                return False
            if cancel_event is not None and cancel_event.is_set():
                raise ProcessCancelledError(proc.args)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            ready, _, _ = select.select([read_fd], [], [], min(0.05, remaining))
            if not ready:
                continue
            marker = os.read(read_fd, 1)
            if marker:
                return True
            if proc.poll() is not None:
                return False
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass


def terminate_process_group(proc: subprocess.Popen, *, timeout: float = 2.0) -> None:
    """Terminate a child and all descendants, escalating after a grace period."""
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        deadline = time.monotonic() + max(0.05, float(timeout))
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except (ProcessLookupError, PermissionError, OSError):
                break
            time.sleep(0.02)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
        return

    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=max(0.05, float(timeout)))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


@dataclass(frozen=True)
class BoundedProcessResult:
    args: Any
    returncode: int
    stdout: str
    stderr: str
    stdout_total_bytes: int
    stderr_total_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool


def run_bounded_process(
    command: Any,
    *,
    cwd: str | pathlib.Path | None = None,
    timeout: float,
    shell: bool = False,
    input_data: bytes | None = None,
    stdout_limit: int = 200_000,
    stderr_limit: int | None = None,
    env: Mapping[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    ready_fd_env_name: str | None = None,
    startup_timeout: float = 5.0,
) -> BoundedProcessResult:
    """Run a process with concurrent, bounded stdout/stderr pipe draining."""
    stdout_capture = BoundedProcessStream(max(1, int(stdout_limit)))
    stderr_capture = BoundedProcessStream(max(1, int(stderr_limit if stderr_limit is not None else stdout_limit)))
    child_env, ready_read_fd, ready_write_fd, ready_popen_kwargs = process_ready_pipe(
        env,
        fd_env_name=ready_fd_env_name,
    )
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            shell=shell,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=child_env,
            **process_group_kwargs(),
            **ready_popen_kwargs,
        )
    except BaseException:
        for fd in (ready_read_fd, ready_write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        raise
    if ready_write_fd is not None:
        try:
            os.close(ready_write_fd)
        except OSError:
            pass

    def drain(pipe: Any, capture: BoundedProcessStream) -> None:
        try:
            while True:
                chunk = pipe.read(65_536)
                if not chunk:
                    break
                capture.feed(chunk)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(target=drain, args=(proc.stdout, stdout_capture), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(proc.stderr, stderr_capture), daemon=True)
    input_thread: threading.Thread | None = None

    def write_input() -> None:
        if proc.stdin is None or input_data is None:
            return
        try:
            view = memoryview(input_data)
            offset = 0
            while offset < len(view):
                written = proc.stdin.write(view[offset:])
                if not written:
                    break
                offset += written
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    stdout_thread.start()
    stderr_thread.start()
    if input_data is not None:
        input_thread = threading.Thread(target=write_input, daemon=True)
        input_thread.start()

    try:
        wait_for_process_ready(
            proc,
            ready_read_fd,
            timeout=startup_timeout,
            cancel_event=cancel_event,
        )
        if cancel_event is None:
            proc.wait(timeout=max(0.01, float(timeout)))
        else:
            deadline = time.monotonic() + max(0.01, float(timeout))
            while proc.poll() is None:
                if cancel_event.is_set():
                    terminate_process_group(proc)
                    if input_thread is not None:
                        input_thread.join(timeout=2)
                    stdout_thread.join(timeout=2)
                    stderr_thread.join(timeout=2)
                    raise ProcessCancelledError(
                        command,
                        output=stdout_capture.text(),
                        stderr=stderr_capture.text(),
                    )
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.02)
    except ProcessCancelledError as exc:
        terminate_process_group(proc)
        if input_thread is not None:
            input_thread.join(timeout=2)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        raise ProcessCancelledError(
            command,
            output=stdout_capture.text(),
            stderr=stderr_capture.text(),
        ) from exc
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(proc)
        if input_thread is not None:
            input_thread.join(timeout=2)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout_capture.text(),
            stderr=stderr_capture.text(),
        ) from exc

    # A successful shell leader may have left descendants holding inherited
    # pipes open. Reap the complete group before joining drainers.
    terminate_process_group(proc, timeout=0.2)
    if input_thread is not None:
        input_thread.join(timeout=2)
    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    return BoundedProcessResult(
        args=command,
        returncode=int(proc.returncode or 0),
        stdout=stdout_capture.text(),
        stderr=stderr_capture.text(),
        stdout_total_bytes=stdout_capture.total,
        stderr_total_bytes=stderr_capture.total,
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )


__all__ = [
    "BoundedProcessResult",
    "BoundedProcessStream",
    "ProcessCancelledError",
    "process_ready_pipe",
    "process_group_kwargs",
    "run_bounded_process",
    "terminate_process_group",
    "wait_for_process_ready",
]
