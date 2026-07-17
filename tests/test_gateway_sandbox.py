from __future__ import annotations

import os
import concurrent.futures
import json
import pathlib
import subprocess
import socket
import sys
import tempfile
import threading
import time
from dataclasses import replace

import pytest

from src import gateway_builtin_tools
from src.gateway_errors import ToolExecutionError
from src.gateway_mcp import _mcp_env
from src.gateway_sandbox import (
    SANDBOX_CONTRACT_VERSION,
    SANDBOX_WORKER_ERROR_PREFIX,
    SANDBOX_WORKER_SETUP_EXIT,
    SandboxDiffEntry,
    SandboxResult,
    sandbox_capabilities,
    sandbox_child_environment,
    sandbox_resource_policy,
    sandbox_isolation_backend,
    sandbox_worker_command,
    workspace_job,
)
from src.gateway_process_ops import run_bounded_process


def test_sandbox_environment_uses_positive_allowlist():
    source = {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "LANG": "C.UTF-8",
        "UPSTREAM_API_KEY": "upstream-secret",
        "GATEWAY_DOWNSTREAM_KEY": "downstream-secret",
        "DATABASE_PASSWORD": "database-secret",
        "CUSTOM_SAFE": "explicit-safe",
        "GATEWAY_TOOL_ENV_ALLOWLIST": "CUSTOM_SAFE",
    }

    env = sandbox_child_environment(source_env=source)

    assert env["PATH"] == "/safe/bin"
    assert env["HOME"] == "/safe/home"
    assert env["CUSTOM_SAFE"] == "explicit-safe"
    assert env["GATEWAY_SANDBOX_CONTRACT_VERSION"] == SANDBOX_CONTRACT_VERSION
    assert "UPSTREAM_API_KEY" not in env
    assert "GATEWAY_DOWNSTREAM_KEY" not in env
    assert "DATABASE_PASSWORD" not in env
    assert "GATEWAY_TOOL_ENV_ALLOWLIST" not in env


def test_explicit_mcp_environment_is_scoped_to_that_server():
    old = os.environ.copy()
    try:
        os.environ.update({
            "PATH": old.get("PATH", "/usr/bin"),
            "UPSTREAM_API_KEY": "gateway-upstream-secret",
            "GATEWAY_DOWNSTREAM_KEY": "gateway-client-secret",
            "MCP_INHERITED_SECRET": "must-not-leak",
        })
        env = _mcp_env({
            "name": "scoped",
            "env": {
                "MCP_API_TOKEN": "server-owned-secret",
                "UPSTREAM_API_KEY": "server-explicit-override",
            },
        })
    finally:
        os.environ.clear()
        os.environ.update(old)

    assert env["MCP_API_TOKEN"] == "server-owned-secret"
    assert env["UPSTREAM_API_KEY"] == "server-explicit-override"
    assert "GATEWAY_DOWNSTREAM_KEY" not in env
    assert "MCP_INHERITED_SECRET" not in env


def test_versioned_job_and_result_contract_redacts_shell_command(tmp_path):
    job = workspace_job(
        "printf secret-command",
        tmp_path,
        shell=True,
        timeout_seconds=3,
        max_output_bytes=4096,
        writable_paths=("result.txt",),
    )
    public = job.as_public_dict()

    assert job.contract_version == SANDBOX_CONTRACT_VERSION
    assert public["command"] == {"kind": "shell", "redacted": True}
    assert public["workspace_root"] == str(tmp_path.resolve())
    assert public["resources"]["timeout_seconds"] == 3
    assert public["writable_paths"] == ("result.txt",)
    assert public["read_policy"] == "system_and_workspace"
    assert public["denied_read_paths"]["redacted"] is True
    assert public["denied_read_paths"]["count"] >= 1

    result = SandboxResult(
        job_id=job.job_id,
        returncode=0,
        stdout="ok",
        stderr="",
        diff=(SandboxDiffEntry("result.txt", "create", None, "abc"),),
    )
    assert result.diff[0].operation == "create"

    capabilities = sandbox_capabilities()
    assert capabilities["backend"] == "local_worker_subprocess"
    assert capabilities["environment"] == "minimal_allowlist_with_admin_explicit_overrides"
    assert capabilities["filesystem_isolation"].startswith("apply_patch_validated_overlay")
    assert capabilities["network_isolation"] == "deny_policy_enforced; inherited_policy_explicit"
    assert capabilities["per_job_resource_limits"]["backend"] == "worker_rlimit_on_supported_posix_platforms"
    assert capabilities["cancellation"] == "process_group_termination"
    assert capabilities["overlay_diff_commit"].startswith("apply_patch_only_validated_hash_diff")


def test_git_and_apply_patch_executables_receive_sanitized_environment(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = bin_dir / "env-probe"
    probe.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s' \"${UPSTREAM_API_KEY-unset}\" \"${GATEWAY_DOWNSTREAM_KEY-unset}\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    git = bin_dir / "git"
    git.symlink_to(probe)
    patch_probe = bin_dir / "patch-env-probe"
    patch_probe.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf 'created\\n' > result.txt\n"
        "printf '%s|%s' \"${UPSTREAM_API_KEY-unset}\" \"${GATEWAY_DOWNSTREAM_KEY-unset}\"\n",
        encoding="utf-8",
    )
    patch_probe.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret-canary")
    monkeypatch.setenv("GATEWAY_DOWNSTREAM_KEY", "downstream-secret-canary")
    monkeypatch.setenv("GATEWAY_TOOL_ENV_ALLOWLIST", "")
    monkeypatch.setenv("GATEWAY_APPLY_PATCH_BIN", str(patch_probe))
    monkeypatch.setattr(gateway_builtin_tools, "_require_write_enabled", lambda: None)

    token = gateway_builtin_tools._WORKSPACE_ROOT_OVERRIDE.set(tmp_path)
    try:
        git_output = gateway_builtin_tools._tool_git({"action": "status"})
        patch_output = gateway_builtin_tools._tool_apply_patch({
            "patch": "*** Begin Patch\n*** Add File: result.txt\n+created\n*** End Patch\n"
        })
    finally:
        gateway_builtin_tools._WORKSPACE_ROOT_OVERRIDE.reset(token)

    assert git_output == "unset|unset"
    assert patch_output.startswith("unset|unset")
    assert "gateway validated diff: create:result.txt" in patch_output
    assert "upstream-secret-canary" not in git_output + patch_output
    assert "downstream-secret-canary" not in git_output + patch_output


def test_short_and_long_lived_resource_policy_defaults():
    short = sandbox_resource_policy(3, source_env={})
    long_lived = sandbox_resource_policy(3, long_lived=True, source_env={})

    if os.name == "nt":
        assert short.cpu_seconds is None
        assert short.max_open_files is None
        assert short.max_file_bytes is None
    else:
        assert short.cpu_seconds == 4
        assert short.max_open_files == 256
        assert short.max_file_bytes == 64 * 1024 * 1024
        assert long_lived.cpu_seconds is None
        assert long_lived.max_open_files == 256
        assert long_lived.max_file_bytes == 64 * 1024 * 1024


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit enforcement")
def test_worker_enforces_file_size_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_SANDBOX_MAX_FILE_BYTES", "1024")
    monkeypatch.setenv("GATEWAY_SANDBOX_CPU_SECONDS", "5")
    target = tmp_path / "oversized.bin"
    job = workspace_job(
        [sys.executable, "-c", f"open({str(target)!r}, 'wb').write(b'X' * 1000000)"],
        tmp_path,
        shell=False,
        timeout_seconds=5,
    )

    result = run_bounded_process(
        sandbox_worker_command(job),
        cwd=tmp_path,
        timeout=5,
        env=sandbox_child_environment(),
    )

    assert result.returncode != 0
    assert target.exists()
    assert target.stat().st_size <= 1024


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit enforcement")
def test_worker_enforces_open_file_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_SANDBOX_MAX_OPEN_FILES", "32")
    job = workspace_job(
        [
            sys.executable,
            "-c",
            "files=[]\ntry:\n while True: files.append(open('/dev/null'))\nexcept OSError:\n print(len(files))",
        ],
        tmp_path,
        shell=False,
        timeout_seconds=5,
    )

    result = run_bounded_process(
        sandbox_worker_command(job),
        cwd=tmp_path,
        timeout=5,
        env=sandbox_child_environment(),
    )

    assert result.returncode == 0
    opened = int(result.stdout.strip())
    assert 1 <= opened < 32


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit enforcement")
def test_worker_cpu_flood_is_stopped_before_wall_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_SANDBOX_CPU_SECONDS", "1")
    job = workspace_job(
        [sys.executable, "-c", "while True: pass"],
        tmp_path,
        shell=False,
        timeout_seconds=8,
    )
    started = time.monotonic()

    result = run_bounded_process(
        sandbox_worker_command(job),
        cwd=tmp_path,
        timeout=8,
        env=sandbox_child_environment(),
    )

    assert result.returncode != 0
    assert time.monotonic() - started < 6


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit enforcement")
def test_memory_limit_is_enforced_or_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_SANDBOX_MEMORY_BYTES", str(512 * 1024 * 1024))
    job = workspace_job(
        [
            sys.executable,
            "-c",
            "try:\n x=bytearray(800_000_000); print('unrestricted')\nexcept MemoryError:\n print('limited')",
        ],
        tmp_path,
        shell=False,
        timeout_seconds=8,
    )

    result = run_bounded_process(
        sandbox_worker_command(job),
        cwd=tmp_path,
        timeout=8,
        env=sandbox_child_environment(),
    )

    if result.returncode == SANDBOX_WORKER_SETUP_EXIT:
        assert result.stderr.startswith(SANDBOX_WORKER_ERROR_PREFIX)
    else:
        assert result.returncode == 0
        assert result.stdout.strip() == "limited"
    assert "unrestricted" not in result.stdout


def test_network_backend_and_escaping_write_scope_fail_closed(tmp_path):
    job = workspace_job(
        [sys.executable, "-c", "print('must-not-run')"],
        tmp_path,
        shell=False,
        timeout_seconds=3,
    )
    sandbox_worker_command(replace(job, network_policy="deny"))
    with pytest.raises(ValueError, match="requires an OS isolation backend"):
        sandbox_worker_command(replace(job, network_policy="deny", isolation_backend="rlimit"))

    with pytest.raises(ValueError, match="writable path escapes workspace"):
        sandbox_worker_command(replace(job, writable_paths=("../outside.txt",)))

    outside = tmp_path.parent / "sandbox-outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "linked-outside").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="writable path escapes workspace"):
        sandbox_worker_command(replace(job, writable_paths=("linked-outside/result.txt",)))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec enforcement")
def test_macos_backend_denies_write_outside_declared_scope(tmp_path, monkeypatch):
    outside = pathlib.Path.home() / f"gateway-sandbox-denied-{os.getpid()}-{time.time_ns()}"
    outside.write_text("host-secret", encoding="utf-8")
    monkeypatch.setenv("GATEWAY_SANDBOX_DENY_READ_PATHS", str(outside))
    inside = tmp_path / "inside.txt"
    code = (
        "import pathlib\n"
        f"pathlib.Path({str(inside)!r}).write_text('inside')\n"
        "try:\n"
        f" print(pathlib.Path({str(outside)!r}).read_text())\n"
        " print('outside-read-succeeded')\n"
        "except OSError:\n"
        " print('outside-read-denied')\n"
        "try:\n"
        f" pathlib.Path({str(outside)!r}).write_text('outside')\n"
        " print('outside-write-succeeded')\n"
        "except OSError:\n"
        " print('outside-write-denied')\n"
    )
    job = workspace_job(
        [sys.executable, "-c", code],
        tmp_path,
        shell=False,
        timeout_seconds=5,
        writable_paths=(".",),
    )

    result = run_bounded_process(
        sandbox_worker_command(job),
        cwd=tmp_path,
        timeout=5,
        env=sandbox_child_environment(),
    )

    assert result.returncode == 0
    assert inside.read_text() == "inside"
    assert "outside-read-denied" in result.stdout
    assert "outside-write-denied" in result.stdout
    assert outside.read_text(encoding="utf-8") == "host-secret"
    outside.unlink()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec enforcement")
def test_macos_backend_enforces_network_deny_and_inherited_modes(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]
    accepted: list[bool] = []

    def accept_one():
        listener.settimeout(3)
        try:
            connection, _address = listener.accept()
            connection.close()
            accepted.append(True)
        except OSError:
            pass

    thread = threading.Thread(target=accept_one, daemon=True)
    thread.start()
    denied = workspace_job(
        [
            sys.executable,
            "-c",
            f"import socket\ntry: socket.create_connection(('127.0.0.1',{port}),1); print('connected')\nexcept OSError: print('denied')",
        ],
        tmp_path,
        shell=False,
        timeout_seconds=5,
        network_policy="deny",
    )
    denied_result = run_bounded_process(
        sandbox_worker_command(denied), cwd=tmp_path, timeout=5, env=sandbox_child_environment()
    )
    assert denied_result.returncode == 0
    assert denied_result.stdout.strip() == "denied"
    assert not accepted

    inherited = replace(denied, network_policy="inherited")
    inherited_result = run_bounded_process(
        sandbox_worker_command(inherited), cwd=tmp_path, timeout=5, env=sandbox_child_environment()
    )
    thread.join(timeout=3)
    listener.close()
    assert inherited_result.returncode == 0
    assert inherited_result.stdout.strip() == "connected"
    assert accepted == [True]


@pytest.mark.skipif(sandbox_isolation_backend() == "rlimit", reason="OS isolation backend required")
def test_concurrent_tenant_jobs_cannot_read_or_write_sibling_workspace(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="gateway-sandbox-tenants-", dir=pathlib.Path.home()) as temp_dir:
        tenant_root = pathlib.Path(temp_dir)
        tenant_a = tenant_root / "tenant-a"
        tenant_b = tenant_root / "tenant-b"
        tenant_a.mkdir()
        tenant_b.mkdir()
        (tenant_a / "secret.txt").write_text("secret-a", encoding="utf-8")
        (tenant_b / "secret.txt").write_text("secret-b", encoding="utf-8")
        monkeypatch.setenv("GATEWAY_SANDBOX_TENANT_ROOT", str(tenant_root))

        def run_job(own: pathlib.Path, sibling: pathlib.Path) -> str:
            code = (
                "import pathlib\n"
                "print(pathlib.Path('secret.txt').read_text())\n"
                "pathlib.Path('own-write.txt').write_text('own')\n"
                "try:\n"
                f" print(pathlib.Path({str(sibling / 'secret.txt')!r}).read_text())\n"
                " print('sibling-read-succeeded')\n"
                "except OSError:\n"
                " print('sibling-read-denied')\n"
                "try:\n"
                f" pathlib.Path({str(sibling / 'cross-write.txt')!r}).write_text('cross')\n"
                " print('sibling-write-succeeded')\n"
                "except OSError:\n"
                " print('sibling-write-denied')\n"
            )
            job = workspace_job(
                [sys.executable, "-c", code],
                own,
                shell=False,
                timeout_seconds=5,
                writable_paths=(".",),
            )
            result = run_bounded_process(
                sandbox_worker_command(job), cwd=own, timeout=5, env=sandbox_child_environment()
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            output_a, output_b = executor.map(
                lambda pair: run_job(*pair),
                ((tenant_a, tenant_b), (tenant_b, tenant_a)),
            )

        assert "secret-a" in output_a
        assert "secret-b" in output_b
        for output in (output_a, output_b):
            assert "sibling-read-denied" in output
            assert "sibling-write-denied" in output
            assert "sibling-read-succeeded" not in output
            assert "sibling-write-succeeded" not in output
        assert (tenant_a / "own-write.txt").read_text() == "own"
        assert (tenant_b / "own-write.txt").read_text() == "own"
        assert not (tenant_a / "cross-write.txt").exists()
        assert not (tenant_b / "cross-write.txt").exists()


def test_worker_rejects_invalid_contract_version(tmp_path):
    worker = pathlib.Path(__file__).parents[1] / "src" / "gateway_sandbox_worker.py"
    policy = {
        "contract_version": "gateway-sandbox-job-v0",
        "workspace_root": str(tmp_path),
        "writable_paths": [],
        "network_policy": "inherited",
        "environment_policy": "minimal_allowlist",
        "resources": {},
    }
    command = {"shell": False, "command": [sys.executable, "-c", "print('must-not-run')"]}
    result = subprocess.run(
        [
            sys.executable,
            str(worker),
            "--policy-json",
            json.dumps(policy),
            "--command-json",
            json.dumps(command),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**sandbox_child_environment(), "GATEWAY_SANDBOX_CONTRACT_VERSION": SANDBOX_CONTRACT_VERSION},
        timeout=5,
        check=False,
    )

    assert result.returncode == SANDBOX_WORKER_SETUP_EXIT
    assert result.stderr.startswith(SANDBOX_WORKER_ERROR_PREFIX)
    assert "must-not-run" not in result.stdout


def test_short_tool_maps_worker_setup_failure(tmp_path, monkeypatch):
    worker = pathlib.Path(__file__).parents[1] / "src" / "gateway_sandbox_worker.py"
    invalid_policy = json.dumps({
        "contract_version": "gateway-sandbox-job-v0",
        "workspace_root": str(tmp_path),
        "writable_paths": [],
        "network_policy": "inherited",
        "environment_policy": "minimal_allowlist",
        "resources": {},
    })
    harmless_command = json.dumps({"shell": False, "command": [sys.executable, "-c", "print('must-not-run')"]})
    monkeypatch.setattr(
        gateway_builtin_tools,
        "sandbox_worker_command",
        lambda _job: [
            sys.executable,
            str(worker),
            "--policy-json",
            invalid_policy,
            "--command-json",
            harmless_command,
        ],
    )
    monkeypatch.setattr(gateway_builtin_tools, "_require_shell_enabled", lambda: None)
    token = gateway_builtin_tools._WORKSPACE_ROOT_OVERRIDE.set(tmp_path)
    try:
        with pytest.raises(ToolExecutionError) as exc_info:
            gateway_builtin_tools._tool_shell({"command": "printf must-not-run", "timeout": 5})
    finally:
        gateway_builtin_tools._WORKSPACE_ROOT_OVERRIDE.reset(token)

    exc = exc_info.value
    assert getattr(exc, "failure_type", "") == "sandbox_setup_failed"
    assert SANDBOX_WORKER_ERROR_PREFIX in str(exc)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal assertion")
def test_worker_command_crash_maps_to_execution_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_builtin_tools, "_require_shell_enabled", lambda: None)
    token = gateway_builtin_tools._WORKSPACE_ROOT_OVERRIDE.set(tmp_path)
    try:
        with pytest.raises(ToolExecutionError) as exc_info:
            gateway_builtin_tools._tool_shell({"command": "kill -SEGV $$", "timeout": 5})
    finally:
        gateway_builtin_tools._WORKSPACE_ROOT_OVERRIDE.reset(token)

    assert exc_info.value.failure_type == "execution_failed"
    assert "exit_code=" in str(exc_info.value)


def test_bundled_apply_patch_fallback_add_update_move_delete(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_APPLY_PATCH_BIN", raising=False)
    real_which = gateway_builtin_tools.shutil.which
    monkeypatch.setattr(
        gateway_builtin_tools.shutil,
        "which",
        lambda name: None if name == "apply_patch" else real_which(name),
    )
    monkeypatch.setattr(gateway_builtin_tools, "_require_write_enabled", lambda: None)
    token = gateway_builtin_tools._WORKSPACE_ROOT_OVERRIDE.set(tmp_path)
    try:
        added = gateway_builtin_tools._tool_apply_patch({
            "patch": (
                "*** Begin Patch\n"
                "*** Add File: nested/value.txt\n"
                "+alpha\n"
                "+beta\n"
                "*** End Patch\n"
            )
        })
        moved = gateway_builtin_tools._tool_apply_patch({
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: nested/value.txt\n"
                "*** Move to: nested/moved.txt\n"
                "@@\n"
                " alpha\n"
                "-beta\n"
                "+gamma\n"
                "*** End Patch\n"
            )
        })
        moved_content = (tmp_path / "nested" / "moved.txt").read_text(encoding="utf-8")
        deleted = gateway_builtin_tools._tool_apply_patch({
            "patch": (
                "*** Begin Patch\n"
                "*** Delete File: nested/moved.txt\n"
                "*** End Patch\n"
            )
        })
    finally:
        gateway_builtin_tools._WORKSPACE_ROOT_OVERRIDE.reset(token)

    assert "create:nested/value.txt" in added
    assert "delete:nested/value.txt" in moved
    assert "create:nested/moved.txt" in moved
    assert moved_content == "alpha\ngamma\n"
    assert (tmp_path / "nested" / "moved.txt").exists() is False
    assert "delete:nested/moved.txt" in deleted
