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
import stat
from pathlib import Path
from unittest.mock import patch

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

    def test_default_config_assumes_weak_upstream_adapter(self):
        """Default upstream must be treated as not supporting tool calls."""
        with patch.dict(
            os.environ,
            {
                "GATEWAY_TOOLS_ENABLED": "",
                "UPSTREAM_SUPPORTS_TOOLS": "",
                "UPSTREAM_SUPPORTS_FUNCTION_CALLS": "",
            },
            clear=False,
        ):
            os.environ.pop("GATEWAY_TOOLS_ENABLED", None)
            os.environ.pop("UPSTREAM_SUPPORTS_TOOLS", None)
            os.environ.pop("UPSTREAM_SUPPORTS_FUNCTION_CALLS", None)
            cfg = gateway._default_config()

        self.assertEqual(cfg["upstream"]["tools_enabled"], "adapter")
        self.assertFalse(cfg["upstream"]["capabilities"]["supports_tools"])
        self.assertFalse(cfg["upstream"]["capabilities"]["supports_function_calls"])
        self.assertEqual(cfg["upstream"]["retry_max_attempts"], 3)
        self.assertEqual(cfg["upstream"]["retry_initial_delay_seconds"], 0.5)
        self.assertEqual(cfg["upstream"]["retry_max_delay_seconds"], 4.0)
        self.assertEqual(cfg["upstream"]["retry_max_elapsed_seconds"], 90.0)
        self.assertEqual(cfg["upstream"]["max_input_tokens"], 1048576)
        self.assertEqual(cfg["upstream"]["max_output_tokens"], 131072)
        self.assertEqual(cfg["upstream"]["max_response_bytes"], 33554432)
        self.assertEqual(cfg["upstream"]["max_stderr_bytes"], 262144)
        self.assertEqual(cfg["upstream"]["max_stream_event_bytes"], 1048576)
        self.assertEqual(cfg["upstream"]["max_stream_events"], 100000)
        self.assertEqual(cfg["mcp"]["max_header_bytes"], 65536)
        self.assertEqual(cfg["mcp"]["max_message_bytes"], 16777216)
        self.assertEqual(cfg["mcp"]["max_stderr_bytes"], 262144)
        self.assertEqual(cfg["gateway"]["rate_limit_backend"], "sqlite")
        self.assertEqual(cfg["gateway"]["rate_limit_fallback_backend"], "memory")
        self.assertEqual(cfg["gateway"]["rate_limit_busy_timeout_ms"], 1000)
        self.assertEqual(cfg["gateway"]["rate_limit_state_ttl_seconds"], 3600)
        self.assertEqual(cfg["gateway"]["concurrency_backend"], "sqlite")
        self.assertEqual(cfg["gateway"]["concurrency_fallback_backend"], "none")
        self.assertEqual(cfg["gateway"]["concurrency_busy_timeout_ms"], 1000)
        self.assertEqual(cfg["gateway"]["concurrency_lease_ttl_seconds"], 120.0)
        self.assertEqual(cfg["gateway"]["concurrency_heartbeat_seconds"], 30.0)
        self.assertFalse(cfg["gateway"]["cors_enabled"])
        self.assertEqual(cfg["gateway"]["cors_allowed_origins"], [])
        self.assertEqual(cfg["gateway"]["max_tool_rounds"], 10)
        self.assertTrue(cfg["gateway"]["agent_planner_strict_every_turn"])
        self.assertTrue(cfg["maintenance"]["enabled"])
        self.assertEqual(cfg["maintenance"]["batch_size"], 1000)
        self.assertEqual(cfg["maintenance"]["request_log_max_rows"], 100000)
        self.assertFalse(cfg["maintenance"]["runtime_cleanup_enabled"])
        self.assertTrue(cfg["maintenance"]["runtime_cleanup_dry_run"])
        self.assertEqual(cfg["context"]["max_input_tokens"], 1048576)
        self.assertEqual(cfg["context"]["fanout_chunk_tokens"], 120000)

    def test_runtime_template_and_compose_defaults_match(self):
        root = Path(__file__).resolve().parent.parent
        runtime = gateway._default_config()
        template = json.loads((root / "gateway.config.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["upstream"]["max_input_tokens"], template["upstream"]["max_input_tokens"])
        self.assertEqual(runtime["upstream"]["max_output_tokens"], template["upstream"]["max_output_tokens"])
        self.assertEqual(runtime["upstream"]["max_response_bytes"], template["upstream"]["max_response_bytes"])
        self.assertEqual(runtime["upstream"]["max_stderr_bytes"], template["upstream"]["max_stderr_bytes"])
        self.assertEqual(runtime["upstream"]["max_stream_event_bytes"], template["upstream"]["max_stream_event_bytes"])
        self.assertEqual(runtime["upstream"]["max_stream_events"], template["upstream"]["max_stream_events"])
        self.assertEqual(runtime["mcp"]["max_header_bytes"], template["mcp"]["max_header_bytes"])
        self.assertEqual(runtime["mcp"]["max_message_bytes"], template["mcp"]["max_message_bytes"])
        self.assertEqual(runtime["mcp"]["max_stderr_bytes"], template["mcp"]["max_stderr_bytes"])
        self.assertEqual(runtime["gateway"]["rate_limit_backend"], template["gateway"]["rate_limit_backend"])
        self.assertEqual(runtime["gateway"]["rate_limit_fallback_backend"], template["gateway"]["rate_limit_fallback_backend"])
        for key in (
            "max_concurrent_requests",
            "concurrency_backend",
            "concurrency_fallback_backend",
            "concurrency_busy_timeout_ms",
            "concurrency_lease_ttl_seconds",
            "concurrency_heartbeat_seconds",
            "concurrency_queue_timeout_seconds",
        ):
            self.assertEqual(runtime["gateway"][key], template["gateway"][key])
        self.assertEqual(runtime["gateway"]["cors_enabled"], template["gateway"]["cors_enabled"])
        self.assertEqual(runtime["gateway"]["cors_allowed_origins"], template["gateway"]["cors_allowed_origins"])
        self.assertEqual(runtime["gateway"]["max_tool_rounds"], template["gateway"]["max_tool_rounds"])
        self.assertEqual(runtime["gateway"]["agent_planner_strict_every_turn"], template["gateway"]["agent_planner_strict_every_turn"])
        self.assertEqual(runtime["maintenance"], template["maintenance"])
        self.assertEqual(runtime["context"]["max_input_tokens"], template["context"]["max_input_tokens"])
        self.assertEqual(runtime["context"]["fanout_chunk_tokens"], template["context"]["fanout_chunk_tokens"])

        for filename in ("docker-compose.yml", "docker-compose.prod.yml"):
            compose = (root / filename).read_text(encoding="utf-8")
            self.assertIn("UPSTREAM_MAX_INPUT_TOKENS=${UPSTREAM_MAX_INPUT_TOKENS:-1048576}", compose)
            self.assertIn("UPSTREAM_MAX_OUTPUT_TOKENS=${UPSTREAM_MAX_OUTPUT_TOKENS:-131072}", compose)
            self.assertIn("UPSTREAM_MAX_RESPONSE_BYTES=${UPSTREAM_MAX_RESPONSE_BYTES:-33554432}", compose)
            self.assertIn("UPSTREAM_MAX_STDERR_BYTES=${UPSTREAM_MAX_STDERR_BYTES:-262144}", compose)
            self.assertIn("UPSTREAM_MAX_STREAM_EVENT_BYTES=${UPSTREAM_MAX_STREAM_EVENT_BYTES:-1048576}", compose)
            self.assertIn("UPSTREAM_MAX_STREAM_EVENTS=${UPSTREAM_MAX_STREAM_EVENTS:-100000}", compose)
            self.assertIn("GATEWAY_MCP_MAX_HEADER_BYTES=${GATEWAY_MCP_MAX_HEADER_BYTES:-65536}", compose)
            self.assertIn("GATEWAY_MCP_MAX_MESSAGE_BYTES=${GATEWAY_MCP_MAX_MESSAGE_BYTES:-16777216}", compose)
            self.assertIn("GATEWAY_MCP_MAX_STDERR_BYTES=${GATEWAY_MCP_MAX_STDERR_BYTES:-262144}", compose)
            self.assertIn("GATEWAY_RATE_LIMIT_BACKEND=${GATEWAY_RATE_LIMIT_BACKEND:-sqlite}", compose)
            self.assertIn("GATEWAY_RATE_LIMIT_FALLBACK_BACKEND=${GATEWAY_RATE_LIMIT_FALLBACK_BACKEND:-memory}", compose)
            self.assertIn("GATEWAY_MAX_CONCURRENT_REQUESTS=${GATEWAY_MAX_CONCURRENT_REQUESTS:-32}", compose)
            self.assertIn("GATEWAY_CONCURRENCY_BACKEND=${GATEWAY_CONCURRENCY_BACKEND:-sqlite}", compose)
            self.assertIn("GATEWAY_CONCURRENCY_FALLBACK_BACKEND=${GATEWAY_CONCURRENCY_FALLBACK_BACKEND:-none}", compose)
            self.assertIn("GATEWAY_CONCURRENCY_LEASE_TTL_SECONDS=${GATEWAY_CONCURRENCY_LEASE_TTL_SECONDS:-120}", compose)
            self.assertIn("GATEWAY_CONCURRENCY_HEARTBEAT_SECONDS=${GATEWAY_CONCURRENCY_HEARTBEAT_SECONDS:-30}", compose)
            self.assertIn("GATEWAY_CORS_ENABLED=${GATEWAY_CORS_ENABLED:-0}", compose)
            self.assertIn("GATEWAY_CORS_ALLOWED_ORIGINS=${GATEWAY_CORS_ALLOWED_ORIGINS:-}", compose)
            self.assertIn("GATEWAY_TOOL_ENV_ALLOWLIST=${GATEWAY_TOOL_ENV_ALLOWLIST:-}", compose)
            self.assertIn("GATEWAY_SANDBOX_ISOLATION_BACKEND=${GATEWAY_SANDBOX_ISOLATION_BACKEND:-auto}", compose)
            self.assertIn("GATEWAY_SANDBOX_READ_POLICY=${GATEWAY_SANDBOX_READ_POLICY:-system_and_workspace}", compose)
            self.assertIn("GATEWAY_SANDBOX_DENY_READ_PATHS=${GATEWAY_SANDBOX_DENY_READ_PATHS:-}", compose)
            self.assertIn("GATEWAY_SANDBOX_TENANT_ROOT=${GATEWAY_SANDBOX_TENANT_ROOT:-/app/workspace}", compose)
            self.assertIn("GATEWAY_SANDBOX_NETWORK_POLICY=${GATEWAY_SANDBOX_NETWORK_POLICY:-inherited}", compose)
            self.assertIn("GATEWAY_SANDBOX_CPU_SECONDS=${GATEWAY_SANDBOX_CPU_SECONDS:-}", compose)
            self.assertIn("GATEWAY_SANDBOX_MEMORY_BYTES=${GATEWAY_SANDBOX_MEMORY_BYTES:-}", compose)
            self.assertIn("GATEWAY_SANDBOX_MAX_PROCESSES=${GATEWAY_SANDBOX_MAX_PROCESSES:-}", compose)
            self.assertIn("GATEWAY_SANDBOX_MAX_OPEN_FILES=${GATEWAY_SANDBOX_MAX_OPEN_FILES:-256}", compose)
            self.assertIn("GATEWAY_SANDBOX_MAX_FILE_BYTES=${GATEWAY_SANDBOX_MAX_FILE_BYTES:-67108864}", compose)
            self.assertIn("GATEWAY_MAX_TOOL_ROUNDS=${GATEWAY_MAX_TOOL_ROUNDS:-10}", compose)
            self.assertIn("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN=${GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN:-1}", compose)
            self.assertIn("GATEWAY_MAINTENANCE_ENABLED=${GATEWAY_MAINTENANCE_ENABLED:-1}", compose)
            self.assertIn("GATEWAY_MAINTENANCE_BATCH_SIZE=${GATEWAY_MAINTENANCE_BATCH_SIZE:-1000}", compose)
            self.assertIn("GATEWAY_REQUEST_LOG_MAX_ROWS=${GATEWAY_REQUEST_LOG_MAX_ROWS:-100000}", compose)
            self.assertIn("GATEWAY_RUNTIME_CLEANUP_ENABLED=${GATEWAY_RUNTIME_CLEANUP_ENABLED:-0}", compose)
            self.assertIn("GATEWAY_RUNTIME_CLEANUP_DRY_RUN=${GATEWAY_RUNTIME_CLEANUP_DRY_RUN:-1}", compose)
            self.assertIn("GATEWAY_CONTEXT_MAX_INPUT_TOKENS=${GATEWAY_CONTEXT_MAX_INPUT_TOKENS:-1048576}", compose)
            self.assertIn("GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS=${GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS:-120000}", compose)

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

    def test_runtime_security_environment_overrides_stale_persisted_credentials(self):
        stale = gateway._default_config()
        stale["admin"] = {
            "username": "admin",
            "password_hash": gateway._hash_password("admin", iterations=1_000),
            "must_change_password": True,
        }
        stale["downstream_keys"] = []
        stale["gateway"]["client_snippet_api_key"] = ""
        self._write(stale)

        with patch.dict(
            os.environ,
            {
                "GATEWAY_ADMIN_PASSWORD": "rotated-runtime-admin",
                "GATEWAY_DOWNSTREAM_KEY": "rotated-runtime-client",
            },
            clear=False,
        ):
            cfg = gateway.load_config()

        self.assertTrue(gateway._verify_password("rotated-runtime-admin", cfg["admin"]["password_hash"]))
        self.assertFalse(cfg["admin"]["must_change_password"])
        self.assertEqual(cfg["gateway"]["client_snippet_api_key"], "rotated-runtime-client")
        expected_hash = gateway._hash_secret("rotated-runtime-client")
        matching = [item for item in cfg["downstream_keys"] if item.get("key_hash") == expected_hash]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0]["enabled"])

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

    def test_admin_password_hash_uses_slow_verifier_and_accepts_legacy_hash(self):
        encoded = gateway._hash_password("correct horse battery staple", iterations=1_000)
        self.assertTrue(encoded.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(gateway._verify_password("correct horse battery staple", encoded))
        self.assertFalse(gateway._verify_password("wrong", encoded))
        self.assertTrue(gateway._password_hash_needs_upgrade(encoded))

        legacy = gateway._hash_secret("legacy-password")
        self.assertTrue(gateway._verify_password("legacy-password", legacy))
        self.assertTrue(gateway._password_hash_needs_upgrade(legacy))

    def test_downstream_key_id_survives_display_name_change(self):
        cfg = gateway._default_config()
        cfg["downstream_keys"] = [{
            "name": "old display name",
            "key_hash": gateway._hash_secret("stable-key"),
            "enabled": True,
        }]
        gateway.save_config(cfg)
        cfg = gateway.load_config()
        first_id = cfg["downstream_keys"][0]["id"]
        cfg["downstream_keys"][0]["name"] = "new display name"
        gateway.save_config(cfg)
        second = gateway.load_config()["downstream_keys"][0]
        self.assertEqual(second["id"], first_id)
        self.assertEqual(second["name"], "new display name")

    def test_config_save_is_owner_only_and_stale_revision_is_rejected(self):
        self._write({})
        cfg, revision = gateway.load_config_with_revision()
        gateway.save_config(cfg, expected_revision=revision)
        self.assertEqual(stat.S_IMODE(os.stat(self._tmp.name).st_mode), 0o600)

        stale_revision = gateway.config_file_revision()
        self._write({"externally_modified": True})
        with self.assertRaises(gateway.ConfigConflictError):
            gateway.save_config(cfg, expected_revision=stale_revision)
        self.assertEqual(self._read(), {"externally_modified": True})

    def test_encryption_failure_does_not_overwrite_existing_config(self):
        self._write({"sentinel": "keep-me"})
        cfg = gateway._default_config()
        cfg["upstream"]["api_key"] = "must-not-be-written"
        with patch("src.gateway_encryption.encrypt_config", side_effect=RuntimeError("disk/key failure")):
            with self.assertRaises(gateway.ConfigError):
                gateway.save_config(cfg)
        self.assertEqual(self._read(), {"sentinel": "keep-me"})


if __name__ == "__main__":
    unittest.main()
