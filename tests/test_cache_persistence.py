#!/usr/bin/env python3
"""Integration test for cache persistence."""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gateway_cache import SemanticCache, ToolResultCache, LocalEmbeddingProvider
from gateway_persistence import (
    PersistenceConfig,
    clear_persistent_caches,
    close_persistence,
    init_persistence,
)


class TestCachePersistence(unittest.TestCase):
    """Test cache persistence integration."""

    def setUp(self):
        """Create temp database for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        config = PersistenceConfig(enabled=True, db_path=self.db_path)
        init_persistence(config)

    def tearDown(self):
        """Clean up temp database."""
        close_persistence()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_semantic_cache_persistence_lifecycle(self):
        """Test semantic cache saves to and loads from database."""
        # Create first cache instance with persistence
        cache1 = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            max_entries=100,
            ttl_seconds=3600,
            persistent=True,
        )

        # Add entries
        cache1.put("What is Python?", {"answer": "A programming language"})
        cache1.put("What is Go?", {"answer": "Another programming language"})

        # Verify in memory
        result = cache1.get("What is Python?")
        self.assertIsNotNone(result)
        self.assertEqual(result["answer"], "A programming language")

        # Destroy cache instance (simulating process restart)
        del cache1

        # Create new cache instance - should load from database
        cache2 = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            max_entries=100,
            ttl_seconds=3600,
            persistent=True,
        )

        # Verify data was persisted
        result = cache2.get("What is Python?")
        self.assertIsNotNone(result)
        self.assertEqual(result["answer"], "A programming language")

        result = cache2.get("What is Go?")
        self.assertIsNotNone(result)
        self.assertEqual(result["answer"], "Another programming language")

    def test_semantic_cache_scope_persists_and_isolates(self):
        """Semantic cache scope survives reload and blocks cross-tenant hits."""
        cache1 = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            max_entries=100,
            similarity_threshold=0.0,
            ttl_seconds=3600,
            persistent=True,
        )
        cache1.put("same prompt", {"answer": "tenant-a"}, scope_key="tenant-a/session/workspace")
        del cache1

        cache2 = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            max_entries=100,
            similarity_threshold=0.0,
            ttl_seconds=3600,
            persistent=True,
        )

        result = cache2.get("same prompt", scope_key="tenant-a/session/workspace")
        self.assertIsNotNone(result)
        self.assertEqual(result["answer"], "tenant-a")
        self.assertIsNone(cache2.get("same prompt", scope_key="tenant-b/session/workspace"))
        self.assertIsNone(cache2.get("similar prompt", scope_key="tenant-b/session/workspace"))

    def test_tool_cache_persistence_lifecycle(self):
        """Test tool cache saves to and loads from database."""
        # Create first cache instance with persistence
        cache1 = ToolResultCache(ttl_seconds=60, persistent=True, persist_local_results=True)

        # Add entries
        cache1.put("Read", {"file_path": "test.txt"}, "file contents here")
        cache1.put("Grep", {"pattern": "foo", "path": "."}, "match results")

        # Verify in memory
        result = cache1.get("Read", {"file_path": "test.txt"})
        self.assertEqual(result, "file contents here")

        # Destroy cache instance
        del cache1

        # Create new cache instance - should load from database
        cache2 = ToolResultCache(ttl_seconds=60, persistent=True, persist_local_results=True)

        # Verify data was persisted (loaded on first get)
        result = cache2.get("Read", {"file_path": "test.txt"})
        self.assertEqual(result, "file contents here")

        result = cache2.get("Grep", {"pattern": "foo", "path": "."})
        self.assertEqual(result, "match results")

    def test_local_tool_results_are_memory_only_by_default(self):
        cache1 = ToolResultCache(ttl_seconds=60, persistent=True)
        cache1.put("Read", {"file_path": "secret.txt"}, "local source")
        cache1.put("WebFetch", {"url": "https://example.test"}, "network response")
        self.assertEqual(cache1.get("Read", {"file_path": "secret.txt"}), "local source")

        cache2 = ToolResultCache(ttl_seconds=60, persistent=True)
        self.assertIsNone(cache2.get("Read", {"file_path": "secret.txt"}))
        self.assertEqual(
            cache2.get("WebFetch", {"url": "https://example.test"}),
            "network response",
        )

    def test_tool_cache_scope_invalidation_persists_and_is_exact(self):
        cache1 = ToolResultCache(ttl_seconds=60, persistent=True, persist_local_results=True)
        args_a = {
            "file_path": "same.txt",
            "__gateway_workspace_cache_key": "/workspace/a",
            "__gateway_runtime_cache_key": "tenant-a/session",
        }
        args_b = {
            "file_path": "same.txt",
            "__gateway_workspace_cache_key": "/workspace/b",
            "__gateway_runtime_cache_key": "tenant-b/session",
        }
        cache1.put("Read", args_a, "A")
        cache1.put("Read", args_b, "B")

        removed = cache1.invalidate_scope("/workspace/a", "tenant-a/session")
        self.assertGreaterEqual(removed, 1)
        self.assertIsNone(cache1.get("Read", args_a))
        self.assertEqual(cache1.get("Read", args_b), "B")

        cache2 = ToolResultCache(ttl_seconds=60, persistent=True, persist_local_results=True)
        self.assertIsNone(cache2.get("Read", args_a))
        self.assertEqual(cache2.get("Read", args_b), "B")

    def test_clear_persistent_caches_removes_both_cache_tables_atomically(self):
        semantic = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            persistent=True,
        )
        tools = ToolResultCache(
            ttl_seconds=60,
            persistent=True,
            persist_local_results=True,
        )
        semantic.put("question", {"answer": "cached"})
        tools.put("Read", {"file_path": "cached.txt"}, "cached file")

        cleared = clear_persistent_caches(strict=True)

        self.assertEqual(cleared, {"semantic": 1, "tools": 1})
        with sqlite3.connect(self.db_path) as conn:
            semantic_count = conn.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
            tool_count = conn.execute("SELECT COUNT(*) FROM tool_cache").fetchone()[0]
        self.assertEqual((semantic_count, tool_count), (0, 0))

    def test_v4_tool_cache_migration_adds_scope_and_drops_opaque_rows(self):
        close_persistence()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            conn.executemany(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                [(version, time.time()) for version in range(1, 5)],
            )
            conn.execute(
                """
                CREATE TABLE tool_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_key TEXT NOT NULL UNIQUE,
                    tool_name TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    result TEXT NOT NULL,
                    success BOOLEAN NOT NULL,
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    access_count INTEGER DEFAULT 0,
                    ttl_seconds INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO tool_cache
                    (cache_key, tool_name, arguments_hash, result, success, created_at,
                     last_accessed, access_count, ttl_seconds)
                VALUES ('Read:legacy', 'Read', 'legacy', 'stale', 1, ?, ?, 0, 60)
                """,
                (time.time(), time.time()),
            )

        init_persistence(PersistenceConfig(enabled=True, db_path=self.db_path))
        with sqlite3.connect(self.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_cache)")}
            count = conn.execute("SELECT COUNT(*) FROM tool_cache").fetchone()[0]
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        self.assertIn("workspace_key", columns)
        self.assertIn("runtime_key", columns)
        self.assertEqual(count, 0)
        self.assertEqual(version, 5)

    def test_cache_expiration_persists(self):
        """Test that expired entries are not loaded from database."""
        # Create cache with short TTL
        cache1 = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            ttl_seconds=1,
            persistent=True,
        )

        cache1.put("short lived", {"data": "expires soon"})

        # Wait for expiration
        time.sleep(1.5)

        # Destroy and recreate
        del cache1
        cache2 = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            ttl_seconds=1,
            persistent=True,
        )

        # Should not load expired entry
        result = cache2.get("short lived")
        self.assertIsNone(result)

    def test_non_persistent_cache_doesnt_save(self):
        """Test that non-persistent cache doesn't save to database."""
        # Create non-persistent cache
        cache1 = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            persistent=False,
        )

        cache1.put("memory only", {"data": "not saved"})

        # Verify in memory
        result = cache1.get("memory only")
        self.assertIsNotNone(result)

        # Destroy and recreate
        del cache1
        cache2 = SemanticCache(
            embedding_provider=LocalEmbeddingProvider(),
            persistent=False,
        )

        # Should not have data from previous instance
        result = cache2.get("memory only")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
