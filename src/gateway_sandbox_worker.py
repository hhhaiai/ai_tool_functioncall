#!/usr/bin/env python3
"""Minimal policy-setting worker for Gateway-owned executable jobs.

The multithreaded Gateway starts this fresh Python process in a new process
group. The worker validates the versioned policy, applies OS resource limits,
and then replaces itself with the requested command. It intentionally imports
only the standard library and never reads Gateway configuration or secrets.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import json
import os
import pathlib
import platform
import shutil
import shlex
import sys
from typing import Any

CONTRACT_VERSION = "gateway-sandbox-job-v1"
SETUP_EXIT = 125
ERROR_PREFIX = "gateway sandbox worker setup failed:"

_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_LL_FS_WRITE_FILE = 1 << 1
_LL_FS_EXECUTE = 1 << 0
_LL_FS_READ_FILE = 1 << 2
_LL_FS_READ_DIR = 1 << 3
_LL_FS_REMOVE_DIR = 1 << 4
_LL_FS_REMOVE_FILE = 1 << 5
_LL_FS_MAKE_CHAR = 1 << 6
_LL_FS_MAKE_DIR = 1 << 7
_LL_FS_MAKE_REG = 1 << 8
_LL_FS_MAKE_SOCK = 1 << 9
_LL_FS_MAKE_FIFO = 1 << 10
_LL_FS_MAKE_BLOCK = 1 << 11
_LL_FS_MAKE_SYM = 1 << 12
_LL_FS_REFER = 1 << 13
_LL_FS_TRUNCATE = 1 << 14


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _fail(message: str) -> int:
    sys.stderr.write(f"{ERROR_PREFIX} {message}\n")
    sys.stderr.flush()
    return SETUP_EXIT


def _positive_optional(value: Any, name: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _apply_resource_limits(resources: dict[str, Any]) -> None:
    requested = {
        "RLIMIT_CPU": _positive_optional(resources.get("cpu_seconds"), "cpu_seconds"),
        "RLIMIT_AS": _positive_optional(resources.get("memory_bytes"), "memory_bytes"),
        "RLIMIT_NPROC": _positive_optional(resources.get("max_processes"), "max_processes"),
        "RLIMIT_NOFILE": _positive_optional(resources.get("max_open_files"), "max_open_files"),
        "RLIMIT_FSIZE": _positive_optional(resources.get("max_file_bytes"), "max_file_bytes"),
    }
    active = {name: value for name, value in requested.items() if value is not None}
    if not active:
        return
    try:
        import resource
    except ImportError as exc:
        raise RuntimeError("resource limits were requested but this platform has no resource module") from exc

    for limit_name, target in active.items():
        limit_id = getattr(resource, limit_name, None)
        if limit_id is None:
            raise RuntimeError(f"requested limit is unsupported on this platform: {limit_name}")
        _soft, hard = resource.getrlimit(limit_id)
        if hard != resource.RLIM_INFINITY and target > hard:
            raise RuntimeError(f"requested {limit_name}={target} exceeds hard limit {hard}")
        resource.setrlimit(limit_id, (target, target))


def _validate_workspace_policy(policy: dict[str, Any]) -> None:
    workspace_root = policy.get("workspace_root")
    if not isinstance(workspace_root, str) or not os.path.isabs(workspace_root):
        raise ValueError("workspace_root must be an absolute path")
    root = os.path.realpath(workspace_root)
    if not os.path.isdir(root):
        raise ValueError("workspace_root must be an existing directory")
    if os.path.realpath(os.getcwd()) != root:
        raise ValueError("worker current directory does not match workspace_root")
    writable_paths = policy.get("writable_paths")
    if not isinstance(writable_paths, list):
        raise ValueError("writable_paths must be a JSON list")
    for raw_path in writable_paths:
        if not isinstance(raw_path, str) or "\x00" in raw_path:
            raise ValueError("writable path must be a string without NUL bytes")
        candidate = raw_path if os.path.isabs(raw_path) else os.path.join(root, raw_path)
        resolved = os.path.realpath(candidate)
        try:
            common = os.path.commonpath((root, resolved))
        except ValueError as exc:
            raise ValueError(f"writable path is invalid: {raw_path}") from exc
        if common != root:
            raise ValueError(f"writable path escapes workspace: {raw_path}")


def _writable_roots(policy: dict[str, Any]) -> list[str]:
    root = os.path.realpath(str(policy["workspace_root"]))
    results: list[str] = []
    for raw_path in policy.get("writable_paths") or []:
        candidate = raw_path if os.path.isabs(raw_path) else os.path.join(root, raw_path)
        resolved = os.path.realpath(candidate)
        if not os.path.exists(resolved):
            resolved = os.path.realpath(os.path.dirname(resolved))
        elif os.path.isfile(resolved):
            resolved = os.path.realpath(os.path.dirname(resolved))
        if os.path.exists(resolved):
            results.append(resolved)
    for candidate in (
        os.environ.get("TMPDIR"),
        os.environ.get("TMP"),
        os.environ.get("TEMP"),
        "/tmp" if sys.platform.startswith("linux") else None,
    ):
        if candidate:
            resolved = os.path.realpath(candidate)
            if os.path.isdir(resolved):
                results.append(resolved)
    deduplicated: list[str] = []
    for value in results:
        if value not in deduplicated:
            deduplicated.append(value)
    return deduplicated


def _readable_roots(policy: dict[str, Any], executable: str, argv: list[str]) -> list[str]:
    if policy.get("read_policy") == "inherited":
        return []
    candidates: list[str | None] = [
        str(policy.get("workspace_root") or ""),
        executable if os.path.isabs(executable) else None,
        sys.prefix,
        sys.base_prefix,
        "/usr",
        "/usr/local",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc",
        "/dev",
    ]
    if sys.platform == "darwin":
        candidates.extend(("/System", "/Library", "/Applications/Xcode.app", "/private/var/db"))
    for directory in str(os.environ.get("PATH") or "").split(os.pathsep):
        if directory:
            candidates.append(directory)
    for item in argv[1:]:
        if isinstance(item, str) and os.path.isabs(item) and os.path.exists(item):
            candidates.append(item)
    candidates.extend(_writable_roots(policy))

    results: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        resolved = os.path.realpath(candidate)
        if os.path.exists(resolved) and resolved not in results:
            results.append(resolved)
    return results


def _denied_read_roots(policy: dict[str, Any]) -> list[str]:
    results: list[str] = []
    for raw_path in policy.get("denied_read_paths") or []:
        if not isinstance(raw_path, str) or not os.path.isabs(raw_path):
            raise ValueError("denied_read_paths must contain absolute paths")
        resolved = os.path.realpath(raw_path)
        if resolved not in results:
            results.append(resolved)
    return results


def _sbpl_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _macos_sandbox_argv(policy: dict[str, Any], executable: str, argv: list[str]) -> tuple[str, list[str]]:
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"):
        raise RuntimeError("macos_sandbox backend is unavailable on this platform")
    clauses = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow file-write* (literal \"/dev/null\"))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
    ]
    clauses.append("(allow file-read*)")
    if policy.get("read_policy") == "system_and_workspace":
        for root in _denied_read_roots(policy):
            quoted = _sbpl_quote(root)
            clauses.append(f'(deny file-read* (literal "{quoted}"))')
            clauses.append(f'(deny file-read* (subpath "{quoted}"))')
    for root in _writable_roots(policy):
        clauses.append(f'(allow file-write* (subpath "{_sbpl_quote(root)}"))')
    if policy.get("network_policy") == "inherited":
        clauses.append("(allow network*)")
    profile = " ".join(clauses)
    sandbox_exec = "/usr/bin/sandbox-exec"
    return sandbox_exec, [sandbox_exec, "-p", profile, executable, *argv[1:]]


def _landlock_syscalls() -> tuple[int, int, int]:
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64", "aarch64", "arm64"}:
        raise RuntimeError(f"unsupported Linux architecture for Landlock syscalls: {machine}")
    return 444, 445, 446


def _set_no_new_privileges(libc: ctypes.CDLL) -> None:
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(error)}")


def _apply_linux_landlock(policy: dict[str, Any], executable: str, argv: list[str]) -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("linux_landlock backend is unavailable on this platform")
    create_ruleset, add_rule, restrict_self = _landlock_syscalls()
    libc = ctypes.CDLL(None, use_errno=True)
    abi = int(libc.syscall(create_ruleset, 0, 0, _LANDLOCK_CREATE_RULESET_VERSION))
    if abi < 1:
        error = ctypes.get_errno()
        raise OSError(error, f"Landlock ABI query failed: {os.strerror(error)}")

    handled = (
        _LL_FS_WRITE_FILE
        | _LL_FS_REMOVE_DIR
        | _LL_FS_REMOVE_FILE
        | _LL_FS_MAKE_CHAR
        | _LL_FS_MAKE_DIR
        | _LL_FS_MAKE_REG
        | _LL_FS_MAKE_SOCK
        | _LL_FS_MAKE_FIFO
        | _LL_FS_MAKE_BLOCK
        | _LL_FS_MAKE_SYM
    )
    if abi >= 2:
        handled |= _LL_FS_REFER
    if abi >= 3:
        handled |= _LL_FS_TRUNCATE
    restricted_reads = policy.get("read_policy") == "system_and_workspace"
    if restricted_reads:
        handled |= _LL_FS_EXECUTE | _LL_FS_READ_FILE | _LL_FS_READ_DIR
    ruleset_attr = _LandlockRulesetAttr(handled_access_fs=handled, handled_access_net=0, scoped=0)
    ruleset_fd = int(libc.syscall(create_ruleset, ctypes.byref(ruleset_attr), ctypes.sizeof(ruleset_attr), 0))
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, f"Landlock ruleset creation failed: {os.strerror(error)}")
    opened: list[int] = []
    try:
        path_access: dict[str, int] = {}
        read_access = _LL_FS_EXECUTE | _LL_FS_READ_FILE | _LL_FS_READ_DIR
        if restricted_reads:
            for path in _readable_roots(policy, executable, argv):
                path_access[path] = path_access.get(path, 0) | read_access
        for path in [*_writable_roots(policy), "/dev/null"]:
            path_access[path] = path_access.get(path, 0) | handled
        for path, requested_access in path_access.items():
            flags = int(getattr(os, "O_PATH", os.O_RDONLY)) | int(getattr(os, "O_CLOEXEC", 0))
            parent_fd = os.open(path, flags)
            opened.append(parent_fd)
            allowed = requested_access
            if not os.path.isdir(path):
                allowed &= _LL_FS_EXECUTE | _LL_FS_READ_FILE | _LL_FS_WRITE_FILE | _LL_FS_TRUNCATE
            rule_attr = _LandlockPathBeneathAttr(allowed_access=allowed, parent_fd=parent_fd)
            rc = int(libc.syscall(add_rule, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule_attr), 0))
            if rc != 0:
                error = ctypes.get_errno()
                raise OSError(error, f"Landlock path rule failed for {path}: {os.strerror(error)}")
        _set_no_new_privileges(libc)
        if int(libc.syscall(restrict_self, ruleset_fd, 0)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, f"Landlock restrict_self failed: {os.strerror(error)}")
    finally:
        for fd in opened:
            os.close(fd)
        os.close(ruleset_fd)


def _apply_linux_network_deny() -> None:
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise RuntimeError("network deny requires libseccomp")
    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    action_allow = 0x7FFF0000
    action_errno = 0x00050000 | errno.EPERM
    context = seccomp.seccomp_init(action_allow)
    if not context:
        raise RuntimeError("seccomp_init failed")
    try:
        for syscall_name in (b"socket", b"socketpair", b"io_uring_setup"):
            number = seccomp.seccomp_syscall_resolve_name(syscall_name)
            if number < 0:
                continue
            rc = seccomp.seccomp_rule_add(context, action_errno, number, 0)
            if rc != 0:
                raise OSError(-rc, f"seccomp rule failed for {syscall_name.decode()}: {os.strerror(-rc)}")
        rc = seccomp.seccomp_load(context)
        if rc != 0:
            raise OSError(-rc, f"seccomp_load failed: {os.strerror(-rc)}")
    finally:
        seccomp.seccomp_release(context)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--policy-json", required=True)
    parser.add_argument("--command-json", required=True)
    return parser.parse_args(argv)


def _command_argv(command_spec: dict[str, Any]) -> tuple[str, list[str]]:
    shell = command_spec.get("shell") is True
    command = command_spec.get("command")
    if shell:
        if not isinstance(command, str) or not command or "\x00" in command:
            raise ValueError("shell command must be a non-empty string without NUL bytes")
        if os.name == "nt":
            executable = os.environ.get("COMSPEC") or "cmd.exe"
            argv = [executable, "/d", "/s", "/c", command]
        else:
            executable = "/bin/sh"
            argv = [executable, "-c", command]
    else:
        if not isinstance(command, list) or not command:
            raise ValueError("argv command must be a non-empty list")
        argv = [str(item) for item in command]
        if not argv[0] or any("\x00" in item for item in argv):
            raise ValueError("argv command contains an empty executable or NUL byte")
        executable = argv[0]
        if not os.path.isabs(executable):
            resolved = shutil.which(executable)
            if not resolved:
                raise FileNotFoundError(f"executable not found on PATH: {executable}")
            executable = resolved
            argv[0] = resolved
        launch_executable = executable
        inspected_executable = os.path.realpath(executable)
        try:
            if os.path.isfile(inspected_executable):
                with open(inspected_executable, "rb") as script_file:
                    first_line = script_file.readline(4096)
                if first_line.startswith(b"#!"):
                    shebang = shlex.split(first_line[2:].decode("utf-8", errors="strict").strip())
                    if not shebang:
                        raise ValueError(f"empty shebang interpreter: {launch_executable}")
                    interpreter = shebang[0]
                    if not os.path.isabs(interpreter):
                        resolved_interpreter = shutil.which(interpreter)
                        if not resolved_interpreter:
                            raise FileNotFoundError(f"shebang interpreter not found: {interpreter}")
                        interpreter = resolved_interpreter
                    interpreter = os.path.realpath(interpreter)
                    argv = [interpreter, *shebang[1:], launch_executable, *argv[1:]]
                    executable = interpreter
                else:
                    executable = launch_executable
                    argv[0] = launch_executable
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid UTF-8 shebang: {launch_executable}") from exc
    return executable, argv


def _exec_command(policy: dict[str, Any], command_spec: dict[str, Any]) -> None:
    executable, argv = _command_argv(command_spec)
    backend = policy.get("isolation_backend")
    if backend == "macos_sandbox":
        executable, argv = _macos_sandbox_argv(policy, executable, argv)
    elif backend == "linux_landlock":
        _apply_linux_landlock(policy, executable, argv)
        if policy.get("network_policy") == "deny":
            _apply_linux_network_deny()
    elif backend == "rlimit":
        if policy.get("network_policy") == "deny":
            raise RuntimeError("network deny is unavailable with the rlimit backend")
    else:
        raise ValueError(f"unsupported isolation backend: {backend}")
    os.execvpe(executable, argv, dict(os.environ))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(list(sys.argv[1:] if argv is None else argv))
        policy = json.loads(args.policy_json)
        command_spec = json.loads(args.command_json)
        if not isinstance(policy, dict) or not isinstance(command_spec, dict):
            raise ValueError("policy and command payloads must be JSON objects")
        if policy.get("contract_version") != CONTRACT_VERSION:
            raise ValueError(f"unsupported contract version: {policy.get('contract_version')}")
        if os.environ.get("GATEWAY_SANDBOX_CONTRACT_VERSION") != CONTRACT_VERSION:
            raise ValueError("child environment contract version is missing or mismatched")
        if policy.get("network_policy") not in {"inherited", "deny"}:
            raise ValueError(f"unsupported network policy: {policy.get('network_policy')}")
        if policy.get("environment_policy") != "minimal_allowlist":
            raise ValueError(f"unsupported environment policy: {policy.get('environment_policy')}")
        if policy.get("read_policy") not in {"inherited", "system_and_workspace"}:
            raise ValueError(f"unsupported read policy: {policy.get('read_policy')}")
        if policy.get("read_policy") == "system_and_workspace" and policy.get("isolation_backend") == "rlimit":
            raise ValueError("restricted read policy requires an OS isolation backend")
        if not isinstance(policy.get("denied_read_paths"), list):
            raise ValueError("denied_read_paths must be a JSON list")
        _validate_workspace_policy(policy)
        resources = policy.get("resources")
        if not isinstance(resources, dict):
            raise ValueError("resources must be a JSON object")
        _apply_resource_limits(resources)
        _exec_command(policy, command_spec)
        raise AssertionError("exec unexpectedly returned")
    except BaseException as exc:
        return _fail(str(exc) or exc.__class__.__name__)


if __name__ == "__main__":
    raise SystemExit(main())
