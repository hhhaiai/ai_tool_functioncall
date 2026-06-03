"""Regression tests for _sync_active_upstream bidirectional sync bug.

The bug: the Admin UI edits the top-level ``upstream`` object, but
``_sync_active_upstream`` would let the stale ``upstream_profiles[0]``
silently overwrite those edits.  These tests pin the corrected behavior.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import gateway_config as gateway


class SyncActiveUpstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = gateway.CONFIG_PATH
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        self._tmp.close()
        gateway.CONFIG_PATH = type(gateway.CONFIG_PATH)(self._tmp.name)

    def tearDown(self) -> None:
        gateway.CONFIG_PATH = self._old_path
        os.unlink(self._tmp.name)

    def _write(self, cfg: dict) -> None:
        with open(self._tmp.name, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)

    def _read(self) -> dict:
        with open(self._tmp.name, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_main_upstream_overrides_stale_profile(self):
        """User edits on the top-level upstream must win over the profile list."""
        self._write({
            "upstream": {
                "base_url": "http://relay:8885",
                "api_key": "sk-test",
                "model": "mimo-v2.5-pro",
                "protocol": "anthropic_messages",
                "tools_enabled": "auto",
                "native_tools_verified": True,
                "capabilities": {
                    "supports_tools": True,
                    "supports_function_calls": True,
                },
                "id": "default",
                "name": "8885",
            },
            "upstream_profiles": [{
                "base_url": "http://relay:8885",
                "api_key": "sk-test",
                "model": "mimo-v2.5-pro",
                "protocol": "openai_chat",
                "tools_enabled": "adapter",
                "native_tools_verified": False,
                "capabilities": {
                    "supports_tools": False,
                    "supports_function_calls": False,
                },
                "id": "default",
                "name": "8885",
            }],
            "active_upstream_id": "default",
        })

        cfg = gateway.load_config()

        self.assertEqual(cfg["upstream"]["protocol"], "anthropic_messages")
        self.assertEqual(cfg["upstream"]["tools_enabled"], "auto")
        self.assertTrue(cfg["upstream"]["native_tools_verified"])
        self.assertTrue(cfg["upstream"]["capabilities"]["supports_tools"])
        self.assertTrue(cfg["upstream"]["capabilities"]["supports_function_calls"])

    def test_profile_specific_overrides_main_unchanged_fields(self):
        """Main is the user-edited source of truth for the active profile.

        If main and profile disagree, main wins for the active id.  This
        matches the Admin UI flow (UI writes main) and the failing-mode that
        the fix targets (stale profile silently overrides user edits).
        """
        self._write({
            "upstream": {
                "base_url": "http://relay:8885",
                "model": "mimo-v2.5-pro",
                "protocol": "anthropic_messages",
                "tools_enabled": "auto",
                "timeout_seconds": 30.0,
                "id": "default",
                "name": "8885",
            },
            "upstream_profiles": [{
                "base_url": "http://relay:8885",
                "api_key": "sk-stale",
                "model": "mimo-v2.5-pro",
                "protocol": "openai_chat",
                "tools_enabled": "adapter",
                "timeout_seconds": 42.0,
                "id": "default",
                "name": "8885",
            }],
            "active_upstream_id": "default",
        })

        cfg = gateway.load_config()

        # Main wins for fields it sets, including timeout_seconds.
        self.assertEqual(cfg["upstream"]["protocol"], "anthropic_messages")
        self.assertEqual(cfg["upstream"]["tools_enabled"], "auto")
        self.assertEqual(cfg["upstream"]["timeout_seconds"], 30.0)

    def test_no_upstream_profalls_back_to_main(self):
        """When profiles is missing, upstream becomes the active profile."""
        self._write({
            "upstream": {
                "base_url": "http://relay:8885",
                "model": "mimo-v2.5-pro",
                "protocol": "anthropic_messages",
                "tools_enabled": "auto",
                "id": "default",
                "name": "8885",
            },
        })

        cfg = gateway.load_config()

        self.assertEqual(cfg["upstream"]["protocol"], "anthropic_messages")
        self.assertEqual(cfg["upstream"]["tools_enabled"], "auto")
        self.assertEqual(len(cfg["upstream_profiles"]), 1)
        self.assertEqual(cfg["upstream_profiles"][0]["protocol"], "anthropic_messages")

    def test_other_profiles_untouched(self):
        """Sync must not modify profiles that do not match the active id."""
        self._write({
            "upstream": {
                "protocol": "anthropic_messages",
                "tools_enabled": "auto",
                "id": "default",
                "name": "8885",
            },
            "upstream_profiles": [
                {
                    "id": "default",
                    "protocol": "openai_chat",
                    "tools_enabled": "adapter",
                },
                {
                    "id": "backup",
                    "base_url": "http://backup:9000",
                    "protocol": "openai_chat",
                    "tools_enabled": "off",
                },
            ],
            "active_upstream_id": "default",
        })

        cfg = gateway.load_config()

        # Active default now has the main values
        self.assertEqual(cfg["upstream"]["protocol"], "anthropic_messages")
        self.assertEqual(cfg["upstream"]["tools_enabled"], "auto")
        # Other profile left alone
        backup = next(p for p in cfg["upstream_profiles"] if p["id"] == "backup")
        self.assertEqual(backup["protocol"], "openai_chat")
        self.assertEqual(backup["base_url"], "http://backup:9000")


if __name__ == "__main__":
    unittest.main()
