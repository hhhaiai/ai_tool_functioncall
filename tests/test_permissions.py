#!/usr/bin/env python3
"""Tests for gateway_permissions module."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gateway_permissions import (
    PermissionRule,
    ClientPermissions,
    PermissionManager,
    check_tool_permission,
    init_permissions,
    TOOL_CATEGORIES,
)


class TestPermissionRule(unittest.TestCase):
    def test_exact_match(self):
        rule = PermissionRule(pattern="Read", allow=True)
        self.assertTrue(rule.matches("Read"))
        self.assertFalse(rule.matches("Write"))
        self.assertFalse(rule.matches("ReadFile"))

    def test_wildcard_match(self):
        rule = PermissionRule(pattern="Read*", allow=True)
        self.assertTrue(rule.matches("Read"))
        self.assertTrue(rule.matches("ReadFile"))
        self.assertTrue(rule.matches("ReadManyFiles"))
        self.assertFalse(rule.matches("Write"))

    def test_star_match_all(self):
        rule = PermissionRule(pattern="*", allow=False)
        self.assertTrue(rule.matches("Read"))
        self.assertTrue(rule.matches("Write"))
        self.assertTrue(rule.matches("Bash"))


class TestClientPermissions(unittest.TestCase):
    def test_default_allow(self):
        client = ClientPermissions(client_id="test", default_allow=True)
        allowed, reason = client.is_allowed("Read")
        self.assertTrue(allowed)
        self.assertIn("default", reason)

    def test_default_deny(self):
        client = ClientPermissions(client_id="test", default_allow=False)
        allowed, reason = client.is_allowed("Read")
        self.assertFalse(allowed)
        self.assertIn("default", reason)

    def test_explicit_rule_allow(self):
        client = ClientPermissions(
            client_id="test",
            rules=[PermissionRule(pattern="Read", allow=True, reason="safe")],
            default_allow=False,
        )
        allowed, reason = client.is_allowed("Read")
        self.assertTrue(allowed)
        self.assertIn("safe", reason)

    def test_explicit_rule_deny(self):
        client = ClientPermissions(
            client_id="test",
            rules=[PermissionRule(pattern="Bash", allow=False, reason="unsafe")],
            default_allow=True,
        )
        allowed, reason = client.is_allowed("Bash")
        self.assertFalse(allowed)
        self.assertIn("unsafe", reason)

    def test_wildcard_rule(self):
        client = ClientPermissions(
            client_id="test",
            rules=[PermissionRule(pattern="Read*", allow=True)],
            default_allow=False,
        )
        self.assertTrue(client.is_allowed("Read")[0])
        self.assertTrue(client.is_allowed("ReadFile")[0])
        self.assertFalse(client.is_allowed("Write")[0])

    def test_rule_precedence(self):
        # First matching rule wins
        client = ClientPermissions(
            client_id="test",
            rules=[
                PermissionRule(pattern="Read", allow=False),
                PermissionRule(pattern="*", allow=True),
            ],
            default_allow=False,
        )
        allowed, reason = client.is_allowed("Read")
        self.assertFalse(allowed)

    def test_category_allow(self):
        client = ClientPermissions(
            client_id="test",
            allow_categories={"read"},
            default_allow=False,
        )
        allowed, reason = client.is_allowed("Read")
        self.assertTrue(allowed)
        self.assertIn("read", reason)

    def test_category_deny(self):
        client = ClientPermissions(
            client_id="test",
            deny_categories={"execute"},
            default_allow=True,
        )
        allowed, reason = client.is_allowed("Bash")
        self.assertFalse(allowed)
        self.assertIn("execute", reason)

    def test_write_category_covers_destructive_file_tools(self):
        client = ClientPermissions(
            client_id="test",
            deny_categories={"write"},
            default_allow=True,
        )
        self.assertFalse(client.is_allowed("DeletePath")[0])
        self.assertFalse(client.is_allowed("MovePath")[0])
        self.assertFalse(client.is_allowed("MultiEdit")[0])

    def test_category_checks_canonical_name_for_aliases(self):
        client = ClientPermissions(
            client_id="test",
            deny_categories={"write"},
            default_allow=True,
        )
        self.assertFalse(client.is_allowed("rm")[0])
        self.assertFalse(client.is_allowed("edit_file")[0])

    def test_category_precedence_over_default(self):
        client = ClientPermissions(
            client_id="test",
            allow_categories={"read"},
            default_allow=False,
        )
        # Read is in "read" category, so should be allowed
        self.assertTrue(client.is_allowed("Read")[0])
        # Write is not in allow_categories, so should be denied
        self.assertFalse(client.is_allowed("Write")[0])

    def test_rule_precedence_over_category(self):
        # Explicit rules take precedence over category rules
        client = ClientPermissions(
            client_id="test",
            rules=[PermissionRule(pattern="Read", allow=False)],
            allow_categories={"read"},
            default_allow=False,
        )
        allowed, reason = client.is_allowed("Read")
        self.assertFalse(allowed)


class TestPermissionManager(unittest.TestCase):
    def test_disabled_manager(self):
        config = {"permissions": {"enabled": False}}
        manager = PermissionManager(config)
        allowed, reason = manager.check_permission("Bash", "client1")
        self.assertTrue(allowed)
        self.assertIn("disabled", reason)

    def test_global_deny_rule(self):
        config = {
            "permissions": {
                "enabled": True,
                "global_rules": [
                    {"pattern": "Bash", "allow": False, "reason": "dangerous"}
                ],
            }
        }
        manager = PermissionManager(config)
        allowed, reason = manager.check_permission("Bash", "client1")
        self.assertFalse(allowed)
        self.assertIn("dangerous", reason)

    def test_global_allow_rule(self):
        config = {
            "permissions": {
                "enabled": True,
                "default_allow": False,
                "global_rules": [
                    {"pattern": "Read*", "allow": True, "reason": "safe"}
                ],
            }
        }
        manager = PermissionManager(config)
        allowed, reason = manager.check_permission("Read", "client1")
        self.assertTrue(allowed)
        self.assertIn("safe", reason)

    def test_client_specific_rules(self):
        config = {
            "permissions": {
                "enabled": True,
                "default_allow": True,
                "clients": {
                    "client1": {
                        "rules": [
                            {"pattern": "Bash", "allow": False}
                        ],
                        "default_allow": True,
                    }
                },
            }
        }
        manager = PermissionManager(config)
        # client1 should be denied Bash
        allowed, reason = manager.check_permission("Bash", "client1")
        self.assertFalse(allowed)
        # client2 should be allowed (no specific rules)
        allowed, reason = manager.check_permission("Bash", "client2")
        self.assertTrue(allowed)

    def test_client_category_permissions(self):
        config = {
            "permissions": {
                "enabled": True,
                "default_allow": False,
                "clients": {
                    "client1": {
                        "allow_categories": ["read", "write"],
                        "deny_categories": ["execute"],
                        "default_allow": False,
                    }
                },
            }
        }
        manager = PermissionManager(config)
        # Read is in "read" category
        self.assertTrue(manager.check_permission("Read", "client1")[0])
        # Write is in "write" category
        self.assertTrue(manager.check_permission("Write", "client1")[0])
        # Bash is in "execute" category (denied)
        self.assertFalse(manager.check_permission("Bash", "client1")[0])

    def test_global_deny_overrides_client_allow(self):
        config = {
            "permissions": {
                "enabled": True,
                "global_rules": [
                    {"pattern": "Bash", "allow": False, "reason": "globally disabled"}
                ],
                "clients": {
                    "client1": {
                        "rules": [
                            {"pattern": "Bash", "allow": True}
                        ],
                    }
                },
            }
        }
        manager = PermissionManager(config)
        # Global deny should take precedence
        allowed, reason = manager.check_permission("Bash", "client1")
        self.assertFalse(allowed)
        self.assertIn("globally disabled", reason)

    def test_get_allowed_tools_default_allow(self):
        config = {
            "permissions": {
                "enabled": True,
                "default_allow": True,
            }
        }
        manager = PermissionManager(config)
        allowed = manager.get_allowed_tools("client1")
        # When default_allow=True, returns empty set (all tools allowed)
        self.assertEqual(allowed, set())

    def test_get_allowed_tools_default_deny(self):
        config = {
            "permissions": {
                "enabled": True,
                "default_allow": False,
                "clients": {
                    "client1": {
                        "rules": [
                            {"pattern": "Read", "allow": True},
                            {"pattern": "Write", "allow": True},
                        ],
                        "default_allow": False,
                    }
                },
            }
        }
        manager = PermissionManager(config)
        allowed = manager.get_allowed_tools("client1")
        self.assertIn("Read", allowed)
        self.assertIn("Write", allowed)

    def test_get_allowed_tools_respects_restrictive_client_when_global_default_allows(self):
        config = {
            "permissions": {
                "enabled": True,
                "default_allow": True,
                "clients": {
                    "client1": {
                        "rules": [{"pattern": "Read", "allow": True}],
                        "default_allow": False,
                    }
                },
            }
        }
        manager = PermissionManager(config)
        self.assertEqual(manager.get_allowed_tools("client1"), {"Read"})

    def test_no_client_config(self):
        config = {
            "permissions": {
                "enabled": True,
                "default_allow": True,
            }
        }
        manager = PermissionManager(config)
        # Unknown client should use default policy
        allowed, reason = manager.check_permission("Read", "unknown_client")
        self.assertTrue(allowed)
        self.assertIn("default", reason)


class TestToolCategories(unittest.TestCase):
    def test_category_structure(self):
        self.assertIn("read", TOOL_CATEGORIES)
        self.assertIn("write", TOOL_CATEGORIES)
        self.assertIn("execute", TOOL_CATEGORIES)
        self.assertIn("network", TOOL_CATEGORIES)
        self.assertIn("system", TOOL_CATEGORIES)

    def test_read_category(self):
        read_tools = TOOL_CATEGORIES["read"]
        self.assertIn("Read", read_tools)
        self.assertIn("Grep", read_tools)
        self.assertIn("Glob", read_tools)

    def test_write_category(self):
        write_tools = TOOL_CATEGORIES["write"]
        self.assertIn("Write", write_tools)
        self.assertIn("Edit", write_tools)

    def test_execute_category(self):
        execute_tools = TOOL_CATEGORIES["execute"]
        self.assertIn("Bash", execute_tools)


class TestGlobalAPI(unittest.TestCase):
    def test_init_and_check(self):
        config = {
            "permissions": {
                "enabled": True,
                "global_rules": [
                    {"pattern": "Bash", "allow": False}
                ],
            }
        }
        init_permissions(config)
        allowed, reason = check_tool_permission("Bash", "test_client", log=False)
        self.assertFalse(allowed)

        allowed, reason = check_tool_permission("Read", "test_client", log=False)
        self.assertTrue(allowed)


class TestComplexScenarios(unittest.TestCase):
    def test_restrictive_client(self):
        """Test a highly restricted client with only read access."""
        config = {
            "permissions": {
                "enabled": True,
                "clients": {
                    "readonly_client": {
                        "allow_categories": ["read"],
                        "default_allow": False,
                    }
                },
            }
        }
        manager = PermissionManager(config)
        # Should allow read tools
        self.assertTrue(manager.check_permission("Read", "readonly_client")[0])
        self.assertTrue(manager.check_permission("Grep", "readonly_client")[0])
        # Should deny write and execute tools
        self.assertFalse(manager.check_permission("Write", "readonly_client")[0])
        self.assertFalse(manager.check_permission("Bash", "readonly_client")[0])

    def test_permissive_client_with_exceptions(self):
        """Test a permissive client with specific denials."""
        config = {
            "permissions": {
                "enabled": True,
                "clients": {
                    "normal_client": {
                        "rules": [
                            {"pattern": "Bash", "allow": False, "reason": "no shell access"},
                        ],
                        "default_allow": True,
                    }
                },
            }
        }
        manager = PermissionManager(config)
        # Should allow most tools
        self.assertTrue(manager.check_permission("Read", "normal_client")[0])
        self.assertTrue(manager.check_permission("Write", "normal_client")[0])
        # But deny Bash
        self.assertFalse(manager.check_permission("Bash", "normal_client")[0])


if __name__ == "__main__":
    unittest.main()
