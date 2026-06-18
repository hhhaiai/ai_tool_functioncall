#!/usr/bin/env python3
"""Configuration encryption module for the gateway.

Provides encryption/decryption of sensitive configuration values
using Fernet (symmetric encryption) from the cryptography library.

Sensitive fields that should be encrypted:
- upstream API keys
- downstream keys
- embedding API keys
- MCP server credentials
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

Json = dict[str, Any]

# Try to import cryptography
try:
    from cryptography.fernet import Fernet
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False
    _logger.warning("cryptography library not available, encryption disabled")


# ---------------------------------------------------------------------------
# Key Management
# ---------------------------------------------------------------------------

def _get_key_path() -> Path:
    """Get the path to the encryption key file."""
    return Path(".gateway_runtime/encryption.key")


def _generate_key() -> bytes:
    """Generate a new Fernet encryption key."""
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library not available")
    return Fernet.generate_key()


def _load_or_create_key() -> bytes:
    """Load existing key or create a new one."""
    key_path = _get_key_path()

    if key_path.exists():
        # Load existing key
        try:
            with open(key_path, "rb") as f:
                key = f.read()
            _logger.info("Loaded encryption key from disk")
            return key
        except Exception as exc:
            _logger.error(f"Failed to load encryption key: {exc}")
            raise

    # Create new key
    key = _generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(key_path, "wb") as f:
            f.write(key)
        # Secure permissions (owner read/write only)
        os.chmod(key_path, 0o600)
        _logger.info(f"Generated new encryption key: {key_path}")
    except Exception as exc:
        _logger.error(f"Failed to save encryption key: {exc}")
        raise

    return key


_encryption_key: bytes | None = None
_fernet: Any | None = None


def _get_fernet() -> Any:
    """Get or create the Fernet cipher instance."""
    global _encryption_key, _fernet

    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library not available")

    if _fernet is None:
        _encryption_key = _load_or_create_key()
        _fernet = Fernet(_encryption_key)

    return _fernet


# ---------------------------------------------------------------------------
# Encryption/Decryption
# ---------------------------------------------------------------------------

def encrypt_value(value: str) -> str:
    """Encrypt a string value.

    Args:
        value: Plain text string to encrypt

    Returns:
        Encrypted value as base64 string with "encrypted:" prefix
    """
    if not _CRYPTO_AVAILABLE:
        _logger.warning("Encryption not available, storing plain text")
        return value

    if not value:
        return value

    try:
        cipher = _get_fernet()
        encrypted_bytes = cipher.encrypt(value.encode())
        encrypted_b64 = base64.b64encode(encrypted_bytes).decode()
        return f"encrypted:{encrypted_b64}"
    except Exception as exc:
        _logger.error(f"Failed to encrypt value: {exc}")
        return value


def decrypt_value(value: str) -> str:
    """Decrypt a string value.

    Args:
        value: Encrypted value (with "encrypted:" prefix) or plain text

    Returns:
        Decrypted plain text string
    """
    if not _CRYPTO_AVAILABLE:
        return value

    if not value or not value.startswith("encrypted:"):
        # Not encrypted, return as-is
        return value

    try:
        encrypted_b64 = value[len("encrypted:"):]
        encrypted_bytes = base64.b64decode(encrypted_b64)
        cipher = _get_fernet()
        decrypted_bytes = cipher.decrypt(encrypted_bytes)
        return decrypted_bytes.decode()
    except Exception as exc:
        _logger.error(f"Failed to decrypt value: {exc}")
        # Return as-is if decryption fails (maybe wrong key)
        return value


def is_encrypted(value: str) -> bool:
    """Check if a value is encrypted.

    Args:
        value: String to check

    Returns:
        True if value has "encrypted:" prefix
    """
    return isinstance(value, str) and value.startswith("encrypted:")


# ---------------------------------------------------------------------------
# Config Encryption
# ---------------------------------------------------------------------------

# Fields that should be encrypted
SENSITIVE_FIELDS = {
    "upstream.api_key",
    "upstream_profiles[].api_key",
    "downstream_keys[].key_hash",  # Note: already hashed, but encrypt anyway
    "cache.embedding_api_key",
    "context.long_context_upstream.api_key",
    "mcp.servers[].env.*",  # Any env vars in MCP servers
}


def _should_encrypt_field(path: str) -> bool:
    """Check if a config field should be encrypted.

    Args:
        path: Dot-separated field path (e.g., "upstream.api_key")

    Returns:
        True if field is sensitive
    """
    # Exact match
    if path in SENSITIVE_FIELDS:
        return True

    # Pattern match (e.g., "upstream_profiles[].api_key")
    for pattern in SENSITIVE_FIELDS:
        if "[]" in pattern:
            # Convert pattern to prefix check
            prefix = pattern.split("[]")[0]
            suffix = pattern.split("[]")[1] if len(pattern.split("[]")) > 1 else ""
            if path.startswith(prefix) and path.endswith(suffix):
                return True

    # Special case: any field named "api_key", "password", "secret", "token"
    field_name = path.split(".")[-1].lower()
    if any(keyword in field_name for keyword in ["api_key", "password", "secret", "token", "key_hash"]):
        return True

    return False


def encrypt_config(config: Json, in_place: bool = False) -> Json:
    """Encrypt sensitive fields in config.

    Args:
        config: Configuration dict
        in_place: If True, modify config in place. Otherwise, create a copy.

    Returns:
        Config with encrypted sensitive fields
    """
    if not _CRYPTO_AVAILABLE:
        return config

    if not in_place:
        import copy
        config = copy.deepcopy(config)

    def _encrypt_recursive(obj: Any, path: str = "") -> Any:
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_path = f"{path}.{key}" if path else key
                obj[key] = _encrypt_recursive(value, current_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                current_path = f"{path}[]"
                obj[i] = _encrypt_recursive(item, current_path)
        elif isinstance(obj, str):
            if _should_encrypt_field(path) and not is_encrypted(obj):
                return encrypt_value(obj)
        return obj

    _encrypt_recursive(config)
    return config


def decrypt_config(config: Json, in_place: bool = False) -> Json:
    """Decrypt encrypted fields in config.

    Args:
        config: Configuration dict with encrypted fields
        in_place: If True, modify config in place. Otherwise, create a copy.

    Returns:
        Config with decrypted fields
    """
    if not _CRYPTO_AVAILABLE:
        return config

    if not in_place:
        import copy
        config = copy.deepcopy(config)

    def _decrypt_recursive(obj: Any) -> Any:
        if isinstance(obj, dict):
            for key, value in obj.items():
                obj[key] = _decrypt_recursive(value)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                obj[i] = _decrypt_recursive(item)
        elif isinstance(obj, str):
            if is_encrypted(obj):
                return decrypt_value(obj)
        return obj

    _decrypt_recursive(config)
    return config


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_config_to_encrypted(config_path: str) -> bool:
    """Migrate a plain-text config file to encrypted format.

    Args:
        config_path: Path to config file

    Returns:
        True if migration succeeded, False otherwise
    """
    if not _CRYPTO_AVAILABLE:
        _logger.error("Cannot migrate: cryptography library not available")
        return False

    try:
        # Load config
        with open(config_path, "r") as f:
            config = json.load(f)

        # Check if already encrypted
        has_encrypted = _check_if_encrypted(config)
        if has_encrypted:
            _logger.info("Config already contains encrypted fields")
            return True

        # Encrypt sensitive fields
        encrypted_config = encrypt_config(config, in_place=False)

        # Backup original
        backup_path = f"{config_path}.backup"
        import shutil
        shutil.copy(config_path, backup_path)
        _logger.info(f"Created backup: {backup_path}")

        # Save encrypted config
        with open(config_path, "w") as f:
            json.dump(encrypted_config, f, indent=2)

        _logger.info(f"Migrated config to encrypted format: {config_path}")
        return True

    except Exception as exc:
        _logger.error(f"Failed to migrate config: {exc}")
        return False


def _check_if_encrypted(config: Json) -> bool:
    """Check if config contains any encrypted values."""
    def _check_recursive(obj: Any) -> bool:
        if isinstance(obj, dict):
            return any(_check_recursive(v) for v in obj.values())
        elif isinstance(obj, list):
            return any(_check_recursive(item) for item in obj)
        elif isinstance(obj, str):
            return is_encrypted(obj)
        return False

    return _check_recursive(config)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "encrypt_value",
    "decrypt_value",
    "is_encrypted",
    "encrypt_config",
    "decrypt_config",
    "migrate_config_to_encrypted",
]
