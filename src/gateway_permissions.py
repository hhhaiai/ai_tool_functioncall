#!/usr/bin/env python3
"""Tool permission system for the gateway.

Provides fine-grained access control for tool execution based on:
- Downstream client key
- Tool name patterns (wildcards supported)
- Tool categories (read/write/network/system)
- Per-client allow/deny lists
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)

Json = dict[str, Any]


# Tool categories for coarse-grained permissions
TOOL_CATEGORIES = {
    "read": {
        "Read", "Grep", "Glob", "ListMcpResourcesTool", "ReadMcpResourceTool",
        "TaskGet", "TaskList", "CronList", "LS", "Tree", "FileInfo",
        "ReadManyFiles", "PythonSymbols", "JsonQuery", "LSP", "view_image",
        "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
        "mcp_list_tools", "mcp_get_prompt",
    },
    "write": {
        "Write", "Edit", "NotebookEdit", "TaskCreate", "TaskUpdate",
        "CronCreate", "CronDelete", "MultiEdit", "RegexEdit", "CopyPath",
        "MovePath", "DeletePath", "CreateDirectory", "TodoWrite", "apply_patch",
    },
    "execute": {
        "Bash", "EnterWorktree", "ExitWorktree", "EnterPlanMode", "ExitPlanMode",
        "Git", "code_interpreter", "exec_shell_start", "exec_wait", "exec_kill",
        "write_stdin",
    },
    "network": {
        "WebFetch", "WebSearch", "Agent",
    },
    "system": {
        "Skill", "ScheduleWakeup", "AskUserQuestion",
    },
}


def _tool_match_names(tool_name: str) -> set[str]:
    """Return user-provided and canonical registry names for permission checks."""
    names = {str(tool_name or "")}
    try:
        try:
            from .gateway_builtin_tools import BUILTIN_TOOLS
        except ImportError:
            try:
                from src.gateway_builtin_tools import BUILTIN_TOOLS
            except ImportError:
                from gateway_builtin_tools import BUILTIN_TOOLS
        tool = BUILTIN_TOOLS.get(tool_name)
        canonical = getattr(tool, "name", "") if tool else ""
        if canonical:
            names.add(str(canonical))
    except Exception:
        pass
    names.discard("")
    return names


@dataclass
class PermissionRule:
    """A single permission rule (allow or deny)."""
    pattern: str  # Tool name pattern (supports wildcards like "Read*", "Bash", "*")
    allow: bool  # True = allow, False = deny
    reason: str = ""  # Optional reason for logging

    def matches(self, tool_name: str) -> bool:
        """Check if this rule matches the given tool name."""
        return any(fnmatch.fnmatch(name, self.pattern) for name in _tool_match_names(tool_name))


@dataclass
class ClientPermissions:
    """Permissions for a specific downstream client."""
    client_id: str  # Client identifier (e.g., downstream key hash)
    rules: list[PermissionRule] = field(default_factory=list)
    allow_categories: set[str] = field(default_factory=set)  # Allowed categories
    deny_categories: set[str] = field(default_factory=set)  # Denied categories
    default_allow: bool = True  # Default behavior if no rules match

    def is_allowed(self, tool_name: str) -> tuple[bool, str]:
        """
        Check if tool is allowed for this client.

        Returns:
            (allowed: bool, reason: str)
        """
        # Check explicit rules first (most specific)
        for rule in self.rules:
            if rule.matches(tool_name):
                reason = rule.reason or (f"matched rule: {rule.pattern}")
                return (rule.allow, reason)

        # Check category-based permissions
        tool_names = _tool_match_names(tool_name)
        for category, tools in TOOL_CATEGORIES.items():
            if tool_names & tools:
                if category in self.deny_categories:
                    return (False, f"category '{category}' is denied")
                if category in self.allow_categories:
                    return (True, f"category '{category}' is allowed")

        # Fall back to default
        reason = "default policy" if self.default_allow else "default deny"
        return (self.default_allow, reason)


class PermissionManager:
    """Manages tool permissions for all downstream clients."""

    def __init__(self, config: Json | None = None):
        """
        Initialize permission manager from config.

        Config format:
        {
            "permissions": {
                "enabled": true,
                "default_allow": true,
                "clients": {
                    "client_key_hash_1": {
                        "rules": [
                            {"pattern": "Bash", "allow": false, "reason": "security"},
                            {"pattern": "Read*", "allow": true}
                        ],
                        "allow_categories": ["read", "write"],
                        "deny_categories": ["execute"],
                        "default_allow": false
                    }
                },
                "global_rules": [
                    {"pattern": "dangerous_tool", "allow": false, "reason": "disabled globally"}
                ]
            }
        }
        """
        config = config or {}
        perm_config = config.get("permissions", {})

        self.enabled = perm_config.get("enabled", False)
        self.default_allow = bool(perm_config.get("default_allow", False))

        # Global rules apply to all clients
        self.global_rules: list[PermissionRule] = []
        for rule_dict in perm_config.get("global_rules", []):
            self.global_rules.append(PermissionRule(
                pattern=rule_dict["pattern"],
                allow=rule_dict.get("allow", True),
                reason=rule_dict.get("reason", ""),
            ))

        # Per-client permissions
        self.clients: dict[str, ClientPermissions] = {}
        for client_id, client_config in perm_config.get("clients", {}).items():
            rules = [
                PermissionRule(
                    pattern=r["pattern"],
                    allow=r.get("allow", True),
                    reason=r.get("reason", ""),
                )
                for r in client_config.get("rules", [])
            ]

            self.clients[client_id] = ClientPermissions(
                client_id=client_id,
                rules=rules,
                allow_categories=set(client_config.get("allow_categories", [])),
                deny_categories=set(client_config.get("deny_categories", [])),
                default_allow=client_config.get("default_allow", self.default_allow),
            )

        # Accept legacy permission maps keyed by display name or key hash, but
        # execute policy checks with the stable downstream key id returned by
        # HTTP authentication.
        for item in config.get("downstream_keys") or []:
            if not isinstance(item, dict):
                continue
            stable_id = str(item.get("id") or "")
            if not stable_id or stable_id in self.clients:
                continue
            for legacy_id in (str(item.get("name") or ""), str(item.get("key_hash") or "")):
                if legacy_id and legacy_id in self.clients:
                    legacy = self.clients[legacy_id]
                    self.clients[stable_id] = ClientPermissions(
                        client_id=stable_id,
                        rules=list(legacy.rules),
                        allow_categories=set(legacy.allow_categories),
                        deny_categories=set(legacy.deny_categories),
                        default_allow=legacy.default_allow,
                    )
                    break

    def check_permission(
        self,
        tool_name: str,
        client_id: str | None = None,
    ) -> tuple[bool, str]:
        """
        Check if tool execution is allowed.

        Args:
            tool_name: Name of the tool to execute
            client_id: Stable downstream key id (`downstream_keys[].id`)

        Returns:
            (allowed: bool, reason: str)
        """
        if not self.enabled:
            return (True, "permissions disabled")

        # Check global deny rules first
        for rule in self.global_rules:
            if rule.matches(tool_name) and not rule.allow:
                reason = rule.reason or f"global deny: {rule.pattern}"
                return (False, reason)

        # Check client-specific permissions
        if client_id and client_id in self.clients:
            client_perms = self.clients[client_id]
            allowed, reason = client_perms.is_allowed(tool_name)
            return (allowed, f"client policy: {reason}")

        # Check global allow rules
        for rule in self.global_rules:
            if rule.matches(tool_name) and rule.allow:
                reason = rule.reason or f"global allow: {rule.pattern}"
                return (True, reason)

        # Fall back to default
        return (self.default_allow, "default policy")

    def get_allowed_tools(self, client_id: str | None = None) -> set[str]:
        """
        Get set of explicitly allowed tool names for a client.
        Returns empty set if all tools are allowed by default.
        """
        if not self.enabled:
            return set()

        allowed = set()
        if client_id and client_id in self.clients:
            client_perms = self.clients[client_id]
            # Only return explicit allows if default is deny
            if not client_perms.default_allow:
                for rule in client_perms.rules:
                    if rule.allow and "*" not in rule.pattern:
                        allowed.add(rule.pattern)
            return allowed

        if self.default_allow:
            return set()

        return allowed

    def log_permission_check(
        self,
        tool_name: str,
        client_id: str | None,
        allowed: bool,
        reason: str,
    ):
        """Log permission check result."""
        if not allowed:
            _logger.warning(
                f"Tool permission denied: tool={tool_name}, "
                f"client={client_id or 'unknown'}, reason={reason}"
            )
        else:
            _logger.debug(
                f"Tool permission granted: tool={tool_name}, "
                f"client={client_id or 'unknown'}, reason={reason}"
            )


# Global permission manager instance
_permission_manager: PermissionManager | None = None


def init_permissions(config: Json | None = None):
    """Initialize the global permission manager."""
    global _permission_manager
    _permission_manager = PermissionManager(config)


def get_permission_manager() -> PermissionManager:
    """Get the global permission manager instance."""
    if _permission_manager is None:
        init_permissions()
    return _permission_manager


def check_tool_permission(
    tool_name: str,
    client_id: str | None = None,
    log: bool = True,
) -> tuple[bool, str]:
    """
    Check if tool execution is permitted.

    Args:
        tool_name: Name of the tool
        client_id: Client identifier
        log: Whether to log the check

    Returns:
        (allowed: bool, reason: str)
    """
    manager = get_permission_manager()
    allowed, reason = manager.check_permission(tool_name, client_id)

    if log:
        manager.log_permission_check(tool_name, client_id, allowed, reason)

    return (allowed, reason)
