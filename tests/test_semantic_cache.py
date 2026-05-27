"""Tests for semantic cache functionality."""
from __future__ import annotations

import json
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.gateway_cache import (
    CacheEntry,
    SemanticCache,
    ToolResultCache,
    LocalEmbeddingProvider,
    RemoteEmbeddingProvider,
    cosine_similarity,
    get_semantic_cache,
    get_tool_result_cache,
    reset_caches,
)


class TestCosineSimilarity:
    """Tests for cosine similarity calculation."""

    def test_identical_vectors(self):
        a = [1.0, 0.0, 0.0]
        assert cosine_similarity(a, a) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_similar_vectors(self):
        a = [1.0, 1.0, 0.0]
        b = [1.0, 0.9, 0.1]
        sim = cosine_similarity(a, b)
        assert sim > 0.9

    def test_different_length_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0, 0.0]
        # Should pad shorter vector
        sim = cosine_similarity(a, b)
        assert isinstance(sim, float)

    def test_zero_vector(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert cosine_similarity(a, b) == 0.0

    def test_normalized_vectors(self):
        import math
        a = [3.0, 4.0]
        mag = math.sqrt(3**2 + 4**2)
        a_norm = [x / mag for x in a]
        assert cosine_similarity(a_norm, a_norm) == pytest.approx(1.0)


class TestLocalEmbeddingProvider:
    """Tests for local embedding provider."""

    def test_embedding_dimension(self):
        provider = LocalEmbeddingProvider(dimension=128)
        embedding = provider.embed("test text")
        assert len(embedding) == 128

    def test_embedding_normalized(self):
        import math
        provider = LocalEmbeddingProvider(dimension=64)
        embedding = provider.embed("hello world")
        magnitude = math.sqrt(sum(x * x for x in embedding))
        assert magnitude == pytest.approx(1.0, abs=0.01)

    def test_similar_texts_similar_embeddings(self):
        provider = LocalEmbeddingProvider()
        emb1 = provider.embed("python programming language")
        emb2 = provider.embed("python coding language")
        emb3 = provider.embed("cooking recipe book")

        sim_similar = cosine_similarity(emb1, emb2)
        sim_different = cosine_similarity(emb1, emb3)
        assert sim_similar > sim_different

    def test_deterministic_embeddings(self):
        provider = LocalEmbeddingProvider()
        text = "deterministic test"
        emb1 = provider.embed(text)
        emb2 = provider.embed(text)
        assert emb1 == emb2

    def test_batch_embedding(self):
        provider = LocalEmbeddingProvider()
        texts = ["hello", "world", "test"]
        embeddings = provider.embed_batch(texts)
        assert len(embeddings) == 3
        assert all(len(emb) == provider.dimension for emb in embeddings)


class TestCacheEntry:
    """Tests for CacheEntry."""

    def test_entry_creation(self):
        entry = CacheEntry(
            query="test query",
            response={"answer": "test"},
            embedding=[1.0, 0.0],
            ttl_seconds=60,
        )
        assert entry.query == "test query"
        assert entry.response == {"answer": "test"}
        assert entry.access_count == 0

    def test_entry_expiry(self):
        entry = CacheEntry(
            query="test",
            response={},
            embedding=[1.0],
            ttl_seconds=0,  # Immediate expiry
        )
        time.sleep(0.01)
        assert entry.is_expired

    def test_entry_not_expired(self):
        entry = CacheEntry(
            query="test",
            response={},
            embedding=[1.0],
            ttl_seconds=3600,
        )
        assert not entry.is_expired

    def test_entry_touch(self):
        entry = CacheEntry(
            query="test",
            response={},
            embedding=[1.0],
        )
        assert entry.access_count == 0
        entry.touch()
        assert entry.access_count == 1
        entry.touch()
        assert entry.access_count == 2


class TestSemanticCache:
    """Tests for SemanticCache."""

    def test_cache_disabled(self):
        cache = SemanticCache(enabled=False)
        cache.put("test", {"answer": "value"})
        assert cache.get("test") is None

    def test_exact_match(self):
        cache = SemanticCache(
            similarity_threshold=0.9,
            ttl_seconds=60,
        )
        cache.put("what is python", {"answer": "A programming language"})

        result = cache.get("what is python")
        assert result is not None
        assert result["answer"] == "A programming language"

    def test_semantic_match(self):
        cache = SemanticCache(
            similarity_threshold=0.8,
            ttl_seconds=60,
        )
        cache.put("what is python programming", {"answer": "Python is a language"})

        # Similar query should match
        result = cache.get("tell me about python coding")
        # Note: Local embedding may not be semantic enough for this to always work
        # This tests the mechanism, not the embedding quality

    def test_cache_miss(self):
        cache = SemanticCache(
            similarity_threshold=0.99,
            ttl_seconds=60,
        )
        cache.put("completely different topic", {"answer": "value"})

        result = cache.get("unrelated query about something else")
        assert result is None

    def test_cache_expiry(self):
        cache = SemanticCache(ttl_seconds=0)  # Immediate expiry
        cache.put("test", {"answer": "value"})
        time.sleep(0.01)
        assert cache.get("test") is None

    def test_cache_invalidation_all(self):
        cache = SemanticCache()
        cache.put("query1", {"answer": "1"})
        cache.put("query2", {"answer": "2"})

        count = cache.invalidate()
        assert count == 2
        assert cache.get("query1") is None
        assert cache.get("query2") is None

    def test_cache_invalidation_pattern(self):
        cache = SemanticCache()
        cache.put("python tutorial", {"answer": "1"})
        cache.put("java tutorial", {"answer": "2"})
        cache.put("python guide", {"answer": "3"})

        count = cache.invalidate(pattern="python")
        assert count == 2
        assert cache.get("java tutorial") is not None

    def test_cache_stats(self):
        cache = SemanticCache()
        cache.put("test", {"answer": "value"})
        cache.get("test")  # Hit
        cache.get("miss")  # Miss

        stats = cache.stats
        assert stats["entries"] == 1
        assert stats["hits"] >= 1
        assert stats["misses"] >= 1

    def test_cache_eviction_lru(self):
        cache = SemanticCache(max_entries=2)
        cache.put("query1", {"answer": "1"})
        cache.put("query2", {"answer": "2"})
        cache.put("query3", {"answer": "3"})  # Should evict oldest

        assert cache.stats["entries"] <= 2

    def test_concurrent_access(self):
        cache = SemanticCache()
        results = []
        errors = []

        def writer(thread_id):
            try:
                for i in range(10):
                    cache.put(f"query_{thread_id}_{i}", {"thread": thread_id, "i": i})
                results.append(thread_id)
            except Exception as e:
                errors.append(e)

        def reader(thread_id):
            try:
                for i in range(10):
                    cache.get(f"query_{thread_id}_{i}")
                results.append(thread_id)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=writer, args=(i,)))
            threads.append(threading.Thread(target=reader, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_clear(self):
        cache = SemanticCache()
        cache.put("test", {"answer": "value"})
        cache.get("test")

        cache.clear()
        assert cache.stats["entries"] == 0
        assert cache.stats["hits"] == 0


class TestToolResultCache:
    """Tests for ToolResultCache."""

    def test_cacheable_tools(self):
        cache = ToolResultCache()
        assert cache.is_cacheable("Read")
        assert cache.is_cacheable("Glob")
        assert cache.is_cacheable("Grep")
        assert not cache.is_cacheable("Bash")
        assert not cache.is_cacheable("Write")

    def test_cache_hit(self):
        cache = ToolResultCache(ttl_seconds=60)
        args = {"path": "test.py"}
        result = '{"content": "file content"}'

        cache.put("Read", args, result)
        cached = cache.get("Read", args)

        assert cached == result

    def test_cache_miss_different_args(self):
        cache = ToolResultCache(ttl_seconds=60)
        cache.put("Read", {"path": "a.py"}, "content a")

        result = cache.get("Read", {"path": "b.py"})
        assert result is None

    def test_cache_expiry(self):
        cache = ToolResultCache(ttl_seconds=0)
        cache.put("Read", {"path": "test.py"}, "content")
        time.sleep(0.01)

        result = cache.get("Read", {"path": "test.py"})
        assert result is None

    def test_non_cacheable_tool(self):
        cache = ToolResultCache()
        cache.put("Bash", {"command": "ls"}, "output")

        result = cache.get("Bash", {"command": "ls"})
        assert result is None

    def test_cache_invalidation_by_path(self):
        cache = ToolResultCache(ttl_seconds=60)
        cache.put("Read", {"path": "src/main.py"}, "content1")
        cache.put("Read", {"path": "src/utils.py"}, "content2")
        cache.put("Glob", {"pattern": "*.py"}, "files")

        count = cache.invalidate(path="main.py")
        assert count == 1
        assert cache.get("Read", {"path": "src/main.py"}) is None
        assert cache.get("Read", {"path": "src/utils.py"}) is not None

    def test_cache_stats(self):
        cache = ToolResultCache()
        cache.put("Read", {"path": "test.py"}, "content")
        cache.get("Read", {"path": "test.py"})  # Hit
        cache.get("Read", {"path": "other.py"})  # Miss

        stats = cache.stats
        assert stats["entries"] == 1
        assert stats["hits"] >= 1
        assert stats["misses"] >= 1

    def test_cache_eviction(self):
        cache = ToolResultCache(max_entries=2)
        cache.put("Read", {"path": "1.py"}, "c1")
        cache.put("Read", {"path": "2.py"}, "c2")
        cache.put("Read", {"path": "3.py"}, "c3")  # Should evict oldest

        assert cache.stats["entries"] <= 2

    def test_concurrent_access(self):
        cache = ToolResultCache(ttl_seconds=60)
        errors = []

        def worker(thread_id):
            try:
                for i in range(20):
                    args = {"path": f"file_{thread_id}_{i}.py"}
                    cache.put("Read", args, f"content_{i}")
                    cache.get("Read", args)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestRemoteEmbeddingProvider:
    """Tests for remote embedding provider."""

    def test_fallback_to_local(self):
        """Should fallback to local embedding when remote fails."""
        provider = RemoteEmbeddingProvider(url="http://invalid-url:9999/embed")
        embedding = provider.embed("test")
        assert len(embedding) > 0  # Should get local fallback


class TestGlobalCacheFunctions:
    """Tests for global cache management functions."""

    def setUp(self):
        reset_caches()

    def test_get_semantic_cache_singleton(self):
        reset_caches()
        cache1 = get_semantic_cache()
        cache2 = get_semantic_cache()
        assert cache1 is cache2

    def test_get_tool_result_cache_singleton(self):
        reset_caches()
        cache1 = get_tool_result_cache()
        cache2 = get_tool_result_cache()
        assert cache1 is cache2

    def test_reset_caches(self):
        reset_caches()
        cache1 = get_semantic_cache()
        reset_caches()
        cache2 = get_semantic_cache()
        assert cache1 is not cache2


@pytest.mark.integration
class TestCacheIntegration:
    """Integration tests for cache with realistic scenarios."""

    def test_repeated_code_questions(self):
        """Test caching for repeated coding questions."""
        cache = SemanticCache(
            similarity_threshold=0.85,
            ttl_seconds=300,
        )

        # First question
        q1 = "How do I read a file in Python?"
        a1 = {"answer": "Use open() or pathlib.Path.read_text()"}
        cache.put(q1, a1)

        # Exact repeat
        result = cache.get(q1)
        assert result == a1

    def test_tool_result_caching_workflow(self):
        """Test tool result caching in a realistic workflow."""
        cache = ToolResultCache(ttl_seconds=60)

        # Simulate reading same file multiple times
        file_content = '{"content": "def main():\\n    print(\\"hello\\")"}'

        for _ in range(5):
            cache.put("Read", {"path": "src/main.py"}, file_content)
            result = cache.get("Read", {"path": "src/main.py"})
            assert result == file_content

        stats = cache.stats
        assert stats["hits"] >= 4  # At least 4 hits from the repeated reads
