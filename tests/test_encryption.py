#!/usr/bin/env python3
"""Tests for gateway_encryption module."""
import json
import os
import tempfile
import concurrent.futures
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from cryptography.fernet import Fernet
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

if CRYPTO_AVAILABLE:
    import gateway_encryption as encryption
    from gateway_encryption import (
        encrypt_value,
        decrypt_value,
        is_encrypted,
        encrypt_config,
        decrypt_config,
        migrate_config_to_encrypted,
    )


@unittest.skipIf(not CRYPTO_AVAILABLE, "cryptography library not available")
class TestEncryption(unittest.TestCase):
    """Test encryption module."""

    def test_encrypt_decrypt_value(self):
        """Test basic encryption and decryption."""
        original = "my-secret-api-key"
        encrypted = encrypt_value(original)

        # Should be encrypted
        self.assertTrue(is_encrypted(encrypted))
        self.assertNotEqual(encrypted, original)
        self.assertTrue(encrypted.startswith("encrypted:"))

        # Should decrypt back to original
        decrypted = decrypt_value(encrypted)
        self.assertEqual(decrypted, original)

    def test_concurrent_first_key_creation_uses_one_atomic_key(self):
        old_key, old_fernet = encryption._encryption_key, encryption._fernet
        try:
            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ,
                {
                    "GATEWAY_ENCRYPTION_KEY_PATH": str(Path(td) / "encryption.key"),
                    "GATEWAY_RUNTIME_DIR": str(Path(td) / "runtime"),
                },
                clear=False,
            ):
                encryption._encryption_key = None
                encryption._fernet = None
                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                    keys = list(executor.map(lambda _: encryption._load_or_create_key(), range(50)))
                self.assertEqual(len(set(keys)), 1)
                key_path = Path(td) / "encryption.key"
                self.assertEqual(key_path.read_bytes(), keys[0])
                self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        finally:
            encryption._encryption_key, encryption._fernet = old_key, old_fernet

    def test_invalid_existing_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"GATEWAY_ENCRYPTION_KEY_PATH": str(Path(td) / "encryption.key")},
            clear=False,
        ):
            key_path = Path(td) / "encryption.key"
            key_path.write_bytes(b"invalid")
            with self.assertRaises(Exception):
                encryption._load_or_create_key()

    def test_decrypt_plain_text(self):
        """Test that plain text is returned as-is."""
        plain = "not-encrypted"
        result = decrypt_value(plain)
        self.assertEqual(result, plain)

    def test_encrypt_empty_string(self):
        """Test encrypting empty string."""
        encrypted = encrypt_value("")
        self.assertEqual(encrypted, "")

    def test_is_encrypted(self):
        """Test encrypted value detection."""
        self.assertFalse(is_encrypted("plain text"))
        self.assertFalse(is_encrypted(""))
        self.assertTrue(is_encrypted("encrypted:abc123"))

    def test_encrypt_config_api_keys(self):
        """Test encrypting API keys in config."""
        config = {
            "upstream": {
                "api_key": "secret-key-123",
                "base_url": "http://example.com",
            },
            "cache": {
                "embedding_api_key": "another-secret",
                "enabled": True,
            },
        }

        encrypted = encrypt_config(config, in_place=False)

        # Should encrypt api_key fields
        self.assertTrue(is_encrypted(encrypted["upstream"]["api_key"]))
        self.assertTrue(is_encrypted(encrypted["cache"]["embedding_api_key"]))

        # Should not encrypt non-sensitive fields
        self.assertEqual(encrypted["upstream"]["base_url"], "http://example.com")
        self.assertEqual(encrypted["cache"]["enabled"], True)

    def test_encrypt_config_nested_lists(self):
        """Test encrypting nested list structures."""
        config = {
            "upstream_profiles": [
                {"name": "profile1", "api_key": "key1"},
                {"name": "profile2", "api_key": "key2"},
            ],
        }

        encrypted = encrypt_config(config, in_place=False)

        # Should encrypt all api_keys in list
        self.assertTrue(is_encrypted(encrypted["upstream_profiles"][0]["api_key"]))
        self.assertTrue(is_encrypted(encrypted["upstream_profiles"][1]["api_key"]))

        # Should not encrypt names
        self.assertEqual(encrypted["upstream_profiles"][0]["name"], "profile1")

    def test_encrypt_config_mcp_env_vars(self):
        """Test encrypting MCP server env vars."""
        config = {
            "mcp": {
                "servers": [
                    {
                        "name": "server1",
                        "env": {
                            "API_KEY": "secret",
                            "DEBUG": "false",
                        },
                    },
                ],
            },
        }

        encrypted = encrypt_config(config, in_place=False)

        # Should encrypt API_KEY
        env = encrypted["mcp"]["servers"][0]["env"]
        self.assertTrue(is_encrypted(env["API_KEY"]))

        # DEBUG is not sensitive, but currently we encrypt all env vars
        # This is conservative but safe

    def test_decrypt_config(self):
        """Test decrypting config."""
        config = {
            "upstream": {
                "api_key": "secret-key",
            },
        }

        # Encrypt then decrypt
        encrypted = encrypt_config(config, in_place=False)
        decrypted = decrypt_config(encrypted, in_place=False)

        # Should restore original values
        self.assertEqual(decrypted["upstream"]["api_key"], "secret-key")

    def test_encrypt_config_in_place(self):
        """Test in-place encryption."""
        config = {
            "upstream": {"api_key": "secret"},
        }

        # Encrypt in place
        encrypt_config(config, in_place=True)

        # Original dict should be modified
        self.assertTrue(is_encrypted(config["upstream"]["api_key"]))

    def test_encrypt_idempotent(self):
        """Test that encrypting already encrypted values is idempotent."""
        original = "secret"
        encrypted1 = encrypt_value(original)
        encrypted2 = encrypt_value(encrypted1)
        self.assertEqual(encrypted2, encrypted1)
        self.assertEqual(decrypt_value(encrypted2), original)

    def test_wrong_encryption_key_fails_closed(self):
        old_key, old_fernet = encryption._encryption_key, encryption._fernet
        try:
            encryption._encryption_key = Fernet.generate_key()
            encryption._fernet = Fernet(encryption._encryption_key)
            encrypted = encrypt_value("secret")
            encryption._encryption_key = Fernet.generate_key()
            encryption._fernet = Fernet(encryption._encryption_key)
            with self.assertRaises(RuntimeError):
                decrypt_value(encrypted)
        finally:
            encryption._encryption_key, encryption._fernet = old_key, old_fernet

    def test_encrypted_config_requires_crypto_support(self):
        with patch.object(encryption, "_CRYPTO_AVAILABLE", False):
            with self.assertRaises(RuntimeError):
                decrypt_config({"upstream": {"api_key": "encrypted:not-valid"}})

    def test_sensitive_field_detection(self):
        """Test automatic detection of sensitive fields."""
        from gateway_encryption import _should_encrypt_field

        # Should detect common sensitive fields
        self.assertTrue(_should_encrypt_field("upstream.api_key"))
        self.assertTrue(_should_encrypt_field("cache.embedding_api_key"))
        self.assertTrue(_should_encrypt_field("admin.password"))
        self.assertTrue(_should_encrypt_field("auth.token"))
        self.assertTrue(_should_encrypt_field("credentials.secret"))

        # Should not encrypt non-sensitive fields
        self.assertFalse(_should_encrypt_field("upstream.base_url"))
        self.assertFalse(_should_encrypt_field("cache.enabled"))
        self.assertFalse(_should_encrypt_field("admin.username"))


@unittest.skipIf(not CRYPTO_AVAILABLE, "cryptography library not available")
class TestConfigMigration(unittest.TestCase):
    """Test config file migration."""

    def test_migrate_config_file(self):
        """Test migrating a plain config to encrypted."""
        # Create temp config file
        temp_dir = tempfile.mkdtemp()
        config_path = os.path.join(temp_dir, "test_config.json")

        plain_config = {
            "upstream": {
                "api_key": "plain-secret",
                "base_url": "http://example.com",
            },
        }

        with open(config_path, "w") as f:
            json.dump(plain_config, f)

        # Migrate
        result = migrate_config_to_encrypted(config_path)
        self.assertTrue(result)

        # Verify backup created
        backup_path = f"{config_path}.backup"
        self.assertTrue(os.path.exists(backup_path))

        # Load encrypted config
        with open(config_path, "r") as f:
            encrypted_config = json.load(f)

        # Should be encrypted
        self.assertTrue(is_encrypted(encrypted_config["upstream"]["api_key"]))

        # Cleanup
        os.remove(config_path)
        os.remove(backup_path)
        os.rmdir(temp_dir)

    def test_migrate_already_encrypted(self):
        """Test migrating already encrypted config."""
        temp_dir = tempfile.mkdtemp()
        config_path = os.path.join(temp_dir, "test_config.json")

        encrypted_config = {
            "upstream": {
                "api_key": "encrypted:abc123",
            },
        }

        with open(config_path, "w") as f:
            json.dump(encrypted_config, f)

        # Migrate (should detect already encrypted)
        result = migrate_config_to_encrypted(config_path)
        self.assertTrue(result)

        # Cleanup
        os.remove(config_path)
        os.rmdir(temp_dir)


if __name__ == "__main__":
    unittest.main()
