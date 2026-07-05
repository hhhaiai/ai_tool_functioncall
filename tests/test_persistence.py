#!/usr/bin/env python3
"""Tests for gateway_persistence module."""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gateway_persistence import (
    PersistenceConfig,
    init_persistence,
    close_persistence,
    save_semantic_cache_entry,
    load_semantic_cache_entries,
    touch_semantic_cache_entry,
    delete_semantic_cache_entry,
    cleanup_expired_semantic_cache,
    save_tool_cache_entry,
    load_tool_cache_entry,
    cleanup_expired_tool_cache,
    save_memory,
    load_memories,
    search_memories,
    delete_memory,
    get_database_stats,
    vacuum_database,
)


class TestPersistence(unittest.TestCase):
    """Test persistence layer."""

    def setUp(self):
        """Create temp database for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        config = PersistenceConfig(
            enabled=True,
            db_path=self.db_path,
        )
        init_persistence(config)

    def tearDown(self):
        """Clean up temp database."""
        close_persistence()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    # -----------------------------------------------------------------------
    # Semantic Cache Tests
    # -----------------------------------------------------------------------

    def test_semantic_cache_save_and_load(self):
        """Test saving and loading semantic cache entries."""
        # Save entry
        cache_key = "test_key_1"
        query = "What is Python?"
        embedding = [0.1, 0.2, 0.3]
        response = {"answer": "Python is a programming language"}
        ttl = 3600

        result = save_semantic_cache_entry(cache_key, query, embedding, response, ttl, scope_key="tenant-a")
        self.assertTrue(result)

        # Load entries
        entries = load_semantic_cache_entries()
        self.assertEqual(len(entries), 1)

        entry = entries[0]
        self.assertEqual(entry["cache_key"], cache_key)
        self.assertEqual(entry["query"], query)
        self.assertEqual(entry["embedding"], embedding)
        self.assertEqual(entry["response"], response)
        self.assertEqual(entry["ttl_seconds"], ttl)
        self.assertEqual(entry["scope_key"], "tenant-a")

    def test_semantic_cache_touch(self):
        """Test updating access stats for semantic cache."""
        cache_key = "test_key_2"
        save_semantic_cache_entry(
            cache_key, "test query", [0.1], {"result": "test"}, 3600
        )

        # Touch the entry
        time.sleep(0.1)
        result = touch_semantic_cache_entry(cache_key)
        self.assertTrue(result)

        # Verify access_count incremented
        entries = load_semantic_cache_entries()
        self.assertEqual(entries[0]["access_count"], 1)

    def test_semantic_cache_delete(self):
        """Test deleting semantic cache entries."""
        cache_key = "test_key_3"
        save_semantic_cache_entry(
            cache_key, "test query", [0.1], {"result": "test"}, 3600
        )

        # Delete
        result = delete_semantic_cache_entry(cache_key)
        self.assertTrue(result)

        # Verify deleted
        entries = load_semantic_cache_entries()
        self.assertEqual(len(entries), 0)

    def test_semantic_cache_cleanup_expired(self):
        """Test cleanup of expired semantic cache entries."""
        # Save entry with short TTL
        save_semantic_cache_entry(
            "expired_key", "test", [0.1], {"result": "test"}, ttl_seconds=1
        )

        # Wait for expiration
        time.sleep(1.5)

        # Cleanup
        deleted = cleanup_expired_semantic_cache()
        self.assertEqual(deleted, 1)

        # Verify empty
        entries = load_semantic_cache_entries()
        self.assertEqual(len(entries), 0)

    def test_semantic_cache_load_with_max_age(self):
        """Test loading semantic cache with age filter."""
        # Save old entry
        save_semantic_cache_entry("old_key", "old", [0.1], {"r": "old"}, 3600)

        # Wait
        time.sleep(0.5)

        # Save new entry
        save_semantic_cache_entry("new_key", "new", [0.2], {"r": "new"}, 3600)

        # Load only recent entries (within 1 second)
        entries = load_semantic_cache_entries(max_age_seconds=1)
        self.assertEqual(len(entries), 2)

        # Load only very recent entries (within 0.3 seconds)
        entries = load_semantic_cache_entries(max_age_seconds=0.3)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["cache_key"], "new_key")

    # -----------------------------------------------------------------------
    # Tool Cache Tests
    # -----------------------------------------------------------------------

    def test_tool_cache_save_and_load(self):
        """Test saving and loading tool cache entries."""
        tool_name = "Read"
        args_hash = "abc123"
        result = "file contents here"
        success = True
        ttl = 30

        # Save
        save_result = save_tool_cache_entry(tool_name, args_hash, result, success, ttl)
        self.assertTrue(save_result)

        # Load
        entry = load_tool_cache_entry(tool_name, args_hash)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["result"], result)
        self.assertEqual(entry["success"], success)
        self.assertEqual(entry["access_count"], 1)  # Incremented on load

    def test_tool_cache_expiration(self):
        """Test tool cache expiration."""
        tool_name = "Read"
        args_hash = "xyz789"

        # Save with short TTL
        save_tool_cache_entry(tool_name, args_hash, "data", True, ttl_seconds=1)

        # Load immediately (should succeed)
        entry = load_tool_cache_entry(tool_name, args_hash)
        self.assertIsNotNone(entry)

        # Wait for expiration
        time.sleep(1.5)

        # Load again (should be None and deleted)
        entry = load_tool_cache_entry(tool_name, args_hash)
        self.assertIsNone(entry)

    def test_tool_cache_cleanup(self):
        """Test cleanup of expired tool cache entries."""
        # Save multiple entries with short TTL
        save_tool_cache_entry("Read", "hash1", "data1", True, ttl_seconds=1)
        save_tool_cache_entry("Grep", "hash2", "data2", True, ttl_seconds=1)

        # Wait for expiration
        time.sleep(1.5)

        # Cleanup
        deleted = cleanup_expired_tool_cache()
        self.assertEqual(deleted, 2)

    # -----------------------------------------------------------------------
    # Memory System Tests
    # -----------------------------------------------------------------------

    def test_memory_save_and_load(self):
        """Test saving and loading memories."""
        memory_id = "mem_001"
        content = "Important project context"
        embedding = [0.5, 0.6, 0.7]
        importance = 0.8
        tags = ["project", "important"]
        metadata = {"source": "conversation", "user": "alice"}

        # Save
        result = save_memory(memory_id, content, embedding, importance, tags, metadata)
        self.assertTrue(result)

        # Load
        memories = load_memories()
        self.assertEqual(len(memories), 1)

        memory = memories[0]
        self.assertEqual(memory["memory_id"], memory_id)
        self.assertEqual(memory["content"], content)
        self.assertEqual(memory["embedding"], embedding)
        self.assertEqual(memory["importance"], importance)
        self.assertEqual(memory["tags"], tags)
        self.assertEqual(memory["metadata"], metadata)

    def test_memory_load_with_filters(self):
        """Test loading memories with filters."""
        # Save multiple memories with different importance
        save_memory("mem1", "Low importance", None, 0.3, [], None)
        save_memory("mem2", "High importance", None, 0.9, [], None)
        save_memory("mem3", "Medium importance", None, 0.6, [], None)

        # Load all
        memories = load_memories()
        self.assertEqual(len(memories), 3)

        # Load with min importance filter
        memories = load_memories(min_importance=0.5)
        self.assertEqual(len(memories), 2)

        # Load with limit
        memories = load_memories(limit=1)
        self.assertEqual(len(memories), 1)
        # Should be highest importance (0.9)
        self.assertEqual(memories[0]["memory_id"], "mem2")

    def test_memory_search(self):
        """Test searching memories by embedding similarity."""
        # Save memories with embeddings
        save_memory("mem1", "Python programming", [1.0, 0.0, 0.0], 0.7, [], None)
        save_memory("mem2", "JavaScript coding", [0.0, 1.0, 0.0], 0.6, [], None)
        save_memory("mem3", "Python scripting", [0.9, 0.1, 0.0], 0.8, [], None)

        # Search with query similar to "Python"
        query_embedding = [1.0, 0.0, 0.0]
        results = search_memories(query_embedding, top_k=2)

        self.assertEqual(len(results), 2)
        # First result should be exact match (mem1)
        self.assertEqual(results[0]["memory_id"], "mem1")
        # Second should be similar (mem3)
        self.assertEqual(results[1]["memory_id"], "mem3")

    def test_memory_delete(self):
        """Test deleting memories."""
        memory_id = "mem_to_delete"
        save_memory(memory_id, "test content", None, 0.5, [], None)

        # Verify exists
        memories = load_memories()
        self.assertEqual(len(memories), 1)

        # Delete
        result = delete_memory(memory_id)
        self.assertTrue(result)

        # Verify deleted
        memories = load_memories()
        self.assertEqual(len(memories), 0)

    # -----------------------------------------------------------------------
    # Maintenance Tests
    # -----------------------------------------------------------------------

    def test_get_database_stats(self):
        """Test getting database statistics."""
        # Add some data
        save_semantic_cache_entry("key1", "q1", [0.1], {"r": "1"}, 3600)
        save_tool_cache_entry("Read", "hash1", "data", True, 30)
        save_memory("mem1", "content", None, 0.5, [], None)

        # Get stats
        stats = get_database_stats()
        self.assertEqual(stats["semantic_cache_entries"], 1)
        self.assertEqual(stats["tool_cache_entries"], 1)
        self.assertEqual(stats["memories"], 1)
        self.assertGreater(stats["size_bytes"], 0)

    def test_vacuum_database(self):
        """Test database vacuum operation."""
        # Add and remove data to create fragmentation
        for i in range(10):
            save_memory(f"mem{i}", f"content{i}", None, 0.5, [], None)

        for i in range(10):
            delete_memory(f"mem{i}")

        # Vacuum (should not raise exception)
        vacuum_database()

        # Verify database still works
        save_memory("after_vacuum", "test", None, 0.5, [], None)
        memories = load_memories()
        self.assertEqual(len(memories), 1)


class TestPersistenceInMemory(unittest.TestCase):
    """Test persistence with in-memory database."""

    def setUp(self):
        """Initialize with memory database."""
        config = PersistenceConfig(enabled=False)  # Uses :memory:
        init_persistence(config)

    def tearDown(self):
        """Clean up."""
        close_persistence()

    def test_memory_database_works(self):
        """Test that in-memory mode works correctly."""
        # Save and load should work
        save_semantic_cache_entry("key1", "query", [0.1], {"r": "1"}, 3600)
        entries = load_semantic_cache_entries()
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
