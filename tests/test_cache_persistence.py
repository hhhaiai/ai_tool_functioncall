#!/usr/bin/env python3
"""Integration test for cache persistence."""
import os
import tempfile
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gateway_cache import SemanticCache, ToolResultCache, LocalEmbeddingProvider
from gateway_persistence import init_persistence, close_persistence, PersistenceConfig


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
        cache1 = ToolResultCache(ttl_seconds=60, persistent=True)

        # Add entries
        cache1.put("Read", {"file_path": "test.txt"}, "file contents here")
        cache1.put("Grep", {"pattern": "foo", "path": "."}, "match results")

        # Verify in memory
        result = cache1.get("Read", {"file_path": "test.txt"})
        self.assertEqual(result, "file contents here")

        # Destroy cache instance
        del cache1

        # Create new cache instance - should load from database
        cache2 = ToolResultCache(ttl_seconds=60, persistent=True)

        # Verify data was persisted (loaded on first get)
        result = cache2.get("Read", {"file_path": "test.txt"})
        self.assertEqual(result, "file contents here")

        result = cache2.get("Grep", {"pattern": "foo", "path": "."})
        self.assertEqual(result, "match results")

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
