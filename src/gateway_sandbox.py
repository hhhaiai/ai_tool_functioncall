#!/usr/bin/env python3
"""Versioned execution-policy contract for Gateway-owned child processes.

This module is the common boundary used before a command reaches a local
subprocess or MCP server.  The current backend provides secret-minimized
environments, bounded output/time semantics through ``gateway_process_ops``,
and an explicit capability description.  Filesystem/network namespaces and
overlay diff commits remain separate capabilities and are reported honestly.
"""
from __future__ import annotations

import os
import pathlib
import json
import math
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SANDBOX_CONTRACT_VERSION = "gateway-sandbox-job-v1"
SANDBOX_WORKER_SETUP_EXIT = 125
SANDBOX_WORKER_ERROR_PREFIX = "gateway sandbox worker setup failed:"

_BASE_ENV_ALLOWLIST = {
    "COLORTERM",
    "COMSPEC",
    "HOME",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
    "WINDIR",
}


def _env_names(values: object) -> set[str]:
    if isinstance(values, str):
        candidates = values.split(",")
    elif isinstance(values, Iterable):
        candidates = list(values)
    else:
        candidates = []
    return {
        str(item).strip()
        for item in candidates
        if str(item).strip() and "=" not in str(item) and "\x00" not in str(item)
    }


def sandbox_child_environment(
    explicit_env: Mapping[str, object] | None = None,
    *,
    additional_allowlist: object = None,
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal child environment without inheriting Gateway secrets.

    ``explicit_env`` is an administrator-owned exception channel, primarily
    for per-MCP credentials.  Generic model/tool arguments never reach it.
    Operators may extend the inherited-name allowlist with
    ``GATEWAY_TOOL_ENV_ALLOWLIST`` or ``additional_allowlist``.
    """
    source = source_env if source_env is not None else os.environ
    allowed = set(_BASE_ENV_ALLOWLIST)
    allowed.update(_env_names(source.get("GATEWAY_TOOL_ENV_ALLOWLIST", "")))
    allowed.update(_env_names(additional_allowlist))
    env = {
        name: str(source[name])
        for name in sorted(allowed)
        if name in source and "\x00" not in str(source[name])
    }
    for raw_name, raw_value in (explicit_env or {}).items():
        name = str(raw_name).strip()
        value = str(raw_value)
        if not name or "=" in name or "\x00" in name or "\x00" in value:
            continue
        env[name] = value
    env["GATEWAY_SANDBOX_CONTRACT_VERSION"] = SANDBOX_CONTRACT_VERSION
    return env


@dataclass(frozen=True)
class SandboxResourcePolicy:
    timeout_seconds: float
    max_stdout_bytes: int = 200_000
    max_stderr_bytes: int = 200_000
    cpu_seconds: int | None = None
    memory_bytes: int | None = None
    max_processes: int | None = None
    max_open_files: int | None = None
    max_file_bytes: int | None = None


def _optional_positive_int(
    name: str,
    *,
    source_env: Mapping[str, str],
    default: int | None = None,
) -> int | None:
    raw = source_env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return value if value > 0 else None


def sandbox_resource_policy(
    timeout_seconds: float,
    *,
    max_stdout_bytes: int = 200_000,
    max_stderr_bytes: int | None = None,
    long_lived: bool = False,
    source_env: Mapping[str, str] | None = None,
) -> SandboxResourcePolicy:
    """Build the enforced worker policy from operator-owned environment values.

    CPU, open-file, and output-file limits receive conservative POSIX defaults
    for short jobs. Long-lived exec/MCP workers do not receive a cumulative CPU
    default, but an explicitly configured CPU limit still applies. Address-space
    and process-count limits remain opt-in because their behavior is materially
    platform and user-account dependent.
    """
    source = source_env if source_env is not None else os.environ
    posix_defaults = os.name != "nt"
    explicit_cpu = source.get("GATEWAY_SANDBOX_CPU_SECONDS")
    if explicit_cpu is None or not str(explicit_cpu).strip():
        cpu_default = None if long_lived or not posix_defaults else max(1, int(math.ceil(float(timeout_seconds))) + 1)
    else:
        cpu_default = None
    stderr_limit = max_stdout_bytes if max_stderr_bytes is None else max_stderr_bytes
    return SandboxResourcePolicy(
        timeout_seconds=float(timeout_seconds),
        max_stdout_bytes=max(1, int(max_stdout_bytes)),
        max_stderr_bytes=max(1, int(stderr_limit)),
        cpu_seconds=_optional_positive_int(
            "GATEWAY_SANDBOX_CPU_SECONDS",
            source_env=source,
            default=cpu_default,
        ),
        memory_bytes=_optional_positive_int("GATEWAY_SANDBOX_MEMORY_BYTES", source_env=source),
        max_processes=_optional_positive_int("GATEWAY_SANDBOX_MAX_PROCESSES", source_env=source),
        max_open_files=_optional_positive_int(
            "GATEWAY_SANDBOX_MAX_OPEN_FILES",
            source_env=source,
            default=256 if posix_defaults else None,
        ),
        max_file_bytes=_optional_positive_int(
            "GATEWAY_SANDBOX_MAX_FILE_BYTES",
            source_env=source,
            default=64 * 1024 * 1024 if posix_defaults else None,
        ),
    )


def sandbox_isolation_backend(source_env: Mapping[str, str] | None = None) -> str:
    source = source_env if source_env is not None else os.environ
    configured = str(source.get("GATEWAY_SANDBOX_ISOLATION_BACKEND") or "auto").strip().lower()
    aliases = {
        "landlock": "linux_landlock",
        "sandbox-exec": "macos_sandbox",
        "sandbox_exec": "macos_sandbox",
        "none": "rlimit",
    }
    configured = aliases.get(configured, configured)
    if configured == "auto":
        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            return "macos_sandbox"
        if sys.platform.startswith("linux"):
            return "linux_landlock"
        return "rlimit"
    if configured not in {"rlimit", "macos_sandbox", "linux_landlock"}:
        raise ValueError(f"unsupported sandbox isolation backend: {configured}")
    return configured


@dataclass(frozen=True)
class SandboxJob:
    command: str | Sequence[str]
    workspace_root: str
    shell: bool
    resources: SandboxResourcePolicy
    job_id: str = field(default_factory=lambda: f"sandbox_{uuid.uuid4().hex}")
    contract_version: str = SANDBOX_CONTRACT_VERSION
    tenant_scope_hash: str = ""
    writable_paths: tuple[str, ...] = ()
    network_policy: str = "inherited"
    environment_policy: str = "minimal_allowlist"
    isolation_backend: str = "auto"
    read_policy: str = "system_and_workspace"
    denied_read_paths: tuple[str, ...] = ()

    def as_public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["denied_read_paths"] = {"count": len(self.denied_read_paths), "redacted": True}
        if self.shell:
            payload["command"] = {"kind": "shell", "redacted": True}
        else:
            argv = [self.command] if isinstance(self.command, str) else list(self.command)
            payload["command"] = {
                "kind": "argv",
                "executable": pathlib.Path(str(argv[0])).name if argv else "",
                "argument_count": max(0, len(argv) - 1),
                "redacted": True,
            }
        return payload


@dataclass(frozen=True)
class SandboxDiffEntry:
    path: str
    operation: str
    before_sha256: str | None = None
    after_sha256: str | None = None


@dataclass(frozen=True)
class SandboxResult:
    job_id: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    diff: tuple[SandboxDiffEntry, ...] = ()
    backend: str = "local_worker_subprocess"


def sandbox_capabilities() -> dict[str, Any]:
    try:
        isolation_backend = sandbox_isolation_backend()
    except ValueError:
        isolation_backend = "invalid_configuration"
    return {
        "contract_version": SANDBOX_CONTRACT_VERSION,
        "backend": "local_worker_subprocess",
        "environment": "minimal_allowlist_with_admin_explicit_overrides",
        "process_tree_cleanup": True,
        "time_limit": True,
        "bounded_output": True,
        "os_isolation_backend": isolation_backend,
        "filesystem_isolation": (
            "apply_patch_validated_overlay_plus_os_write_scope"
            if isolation_backend in {"macos_sandbox", "linux_landlock"}
            else "apply_patch_validated_overlay; other_tools_shared_workspace_controls_only"
        ),
        "network_isolation": (
            "deny_policy_enforced; inherited_policy_explicit"
            if isolation_backend in {"macos_sandbox", "linux_landlock"}
            else "deny_policy_fail_closed_not_available"
        ),
        "read_isolation": {
            "linux_landlock": "system_runtime_command_dependencies_workspace_and_temp_allowlist",
            "macos_sandbox": "explicit_sensitive_path_denylist_enforced_by_sandbox_profile",
            "rlimit": "restricted_read_policy_fail_closed_not_available",
            "invalid_configuration": "invalid_configuration",
        }.get(isolation_backend, "unavailable"),
        "per_job_resource_limits": {
            "backend": "worker_rlimit_on_supported_posix_platforms",
            "cpu_seconds": "enforced_for_short_jobs_by_default; explicit_for_long_lived_jobs",
            "max_open_files": "enforced_by_default_on_posix",
            "max_file_bytes": "enforced_by_default_on_posix",
            "memory_bytes": "enforced_when_configured_and_supported",
            "max_processes": "enforced_when_configured_and_supported",
        },
        "cancellation": "process_group_termination",
        "overlay_diff_commit": "apply_patch_only_validated_hash_diff_with_conflict_check_and_transactional_rollback",
    }


def validate_sandbox_job(job: SandboxJob) -> None:
    if job.contract_version != SANDBOX_CONTRACT_VERSION:
        raise ValueError(f"unsupported sandbox contract version: {job.contract_version}")
    workspace = pathlib.Path(job.workspace_root)
    if not workspace.is_absolute() or not workspace.exists() or not workspace.is_dir():
        raise ValueError("sandbox workspace_root must be an existing absolute directory")
    if job.network_policy != "inherited":
        if job.network_policy != "deny":
            raise ValueError(f"unsupported sandbox network policy: {job.network_policy}")
        if job.isolation_backend == "rlimit":
            raise ValueError("sandbox network deny policy requires an OS isolation backend")
    if job.isolation_backend not in {"rlimit", "macos_sandbox", "linux_landlock"}:
        raise ValueError(f"unsupported sandbox isolation backend: {job.isolation_backend}")
    if job.read_policy not in {"inherited", "system_and_workspace"}:
        raise ValueError(f"unsupported sandbox read policy: {job.read_policy}")
    if job.read_policy == "system_and_workspace" and job.isolation_backend == "rlimit":
        raise ValueError("restricted sandbox read policy requires an OS isolation backend")
    command = job.command
    if job.shell:
        if not isinstance(command, str) or not command:
            raise ValueError("sandbox shell command must be a non-empty string")
    else:
        argv = [command] if isinstance(command, str) else list(command)
        if not argv or not str(argv[0]):
            raise ValueError("sandbox argv command must contain an executable")
        if any("\x00" in str(item) for item in argv):
            raise ValueError("sandbox argv contains a NUL byte")
    resources = job.resources
    if resources.timeout_seconds <= 0:
        raise ValueError("sandbox timeout_seconds must be positive")
    for field_name in (
        "max_stdout_bytes",
        "max_stderr_bytes",
        "cpu_seconds",
        "memory_bytes",
        "max_processes",
        "max_open_files",
        "max_file_bytes",
    ):
        value = getattr(resources, field_name)
        if value is not None and int(value) <= 0:
            raise ValueError(f"sandbox {field_name} must be positive when configured")
    root = workspace.resolve()
    for raw_path in job.writable_paths:
        candidate = pathlib.Path(raw_path)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"sandbox writable path escapes workspace: {raw_path}") from exc
    for raw_path in job.denied_read_paths:
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path or not pathlib.Path(raw_path).is_absolute():
            raise ValueError("sandbox denied read paths must be absolute strings without NUL bytes")


def sandbox_worker_command(job: SandboxJob) -> list[str]:
    """Return an argv that applies the job policy and then execs its command."""
    validate_sandbox_job(job)
    worker = pathlib.Path(__file__).with_name("gateway_sandbox_worker.py")
    policy = {
        "contract_version": job.contract_version,
        "workspace_root": job.workspace_root,
        "writable_paths": list(job.writable_paths),
        "network_policy": job.network_policy,
        "environment_policy": job.environment_policy,
        "isolation_backend": job.isolation_backend,
        "read_policy": job.read_policy,
        "denied_read_paths": list(job.denied_read_paths),
        "resources": asdict(job.resources),
    }
    command: str | list[str]
    if job.shell:
        command = str(job.command)
    else:
        command = [str(item) for item in ([job.command] if isinstance(job.command, str) else job.command)]
    return [
        sys.executable,
        str(worker),
        "--policy-json",
        json.dumps(policy, separators=(",", ":")),
        "--command-json",
        json.dumps({"shell": job.shell, "command": command}, separators=(",", ":")),
    ]


def workspace_job(
    command: str | Sequence[str],
    workspace_root: str | pathlib.Path,
    *,
    shell: bool,
    timeout_seconds: float,
    max_output_bytes: int = 200_000,
    writable_paths: Sequence[str] = (),
    long_lived: bool = False,
    network_policy: str | None = None,
    read_policy: str | None = None,
) -> SandboxJob:
    isolation_backend = sandbox_isolation_backend()
    resolved_workspace = pathlib.Path(workspace_root).resolve()
    effective_network_policy = str(
        network_policy
        if network_policy is not None
        else os.environ.get("GATEWAY_SANDBOX_NETWORK_POLICY") or "inherited"
    ).strip().lower()
    effective_read_policy = str(
        read_policy
        if read_policy is not None
        else os.environ.get("GATEWAY_SANDBOX_READ_POLICY")
        or ("inherited" if isolation_backend == "rlimit" else "system_and_workspace")
    ).strip().lower()
    denied_read_paths: list[str] = []
    for env_name in (
        "GATEWAY_CONFIG_PATH",
        "GATEWAY_SQLITE_LOG_PATH",
        "GATEWAY_RUNTIME_DIR",
        "GATEWAY_REQUEST_LOG",
        "GATEWAY_STATS_PATH",
    ):
        value = os.environ.get(env_name)
        if value:
            denied_read_paths.append(str(pathlib.Path(value).expanduser().resolve(strict=False)))
    home = pathlib.Path.home()
    denied_read_paths.extend(str(path.resolve(strict=False)) for path in (
        home / ".ssh",
        home / ".aws",
        home / ".azure",
        home / ".kube",
        home / ".config" / "gcloud",
        home / ".docker" / "config.json",
        home / ".netrc",
        home / ".npmrc",
        home / ".pypirc",
    ))
    for raw_path in str(os.environ.get("GATEWAY_SANDBOX_DENY_READ_PATHS") or "").split(","):
        if raw_path.strip():
            denied_read_paths.append(str(pathlib.Path(raw_path.strip()).expanduser().resolve(strict=False)))
    tenant_root_value = os.environ.get("GATEWAY_SANDBOX_TENANT_ROOT") or os.environ.get("GATEWAY_WORKSPACE_ROOT")
    if tenant_root_value:
        tenant_root = pathlib.Path(tenant_root_value).expanduser().resolve(strict=False)
        try:
            relative_workspace = resolved_workspace.relative_to(tenant_root)
        except ValueError:
            relative_workspace = None
        if relative_workspace is not None and relative_workspace.parts:
            active_top_level = tenant_root / relative_workspace.parts[0]
            try:
                for sibling in tenant_root.iterdir():
                    if sibling.resolve(strict=False) != active_top_level.resolve(strict=False):
                        denied_read_paths.append(str(sibling.resolve(strict=False)))
            except OSError as exc:
                raise ValueError(f"failed to enumerate sandbox tenant root: {tenant_root}") from exc
    return SandboxJob(
        command=command,
        workspace_root=str(resolved_workspace),
        shell=bool(shell),
        resources=sandbox_resource_policy(
            timeout_seconds,
            max_stdout_bytes=max_output_bytes,
            max_stderr_bytes=max_output_bytes,
            long_lived=long_lived,
        ),
        writable_paths=tuple(str(path) for path in writable_paths),
        network_policy=effective_network_policy,
        isolation_backend=isolation_backend,
        read_policy=effective_read_policy,
        denied_read_paths=tuple(dict.fromkeys(denied_read_paths)),
    )


__all__ = [
    "SANDBOX_CONTRACT_VERSION",
    "SANDBOX_WORKER_ERROR_PREFIX",
    "SANDBOX_WORKER_SETUP_EXIT",
    "SandboxDiffEntry",
    "SandboxJob",
    "SandboxResourcePolicy",
    "SandboxResult",
    "sandbox_capabilities",
    "sandbox_child_environment",
    "sandbox_resource_policy",
    "sandbox_isolation_backend",
    "sandbox_worker_command",
    "validate_sandbox_job",
    "workspace_job",
]
