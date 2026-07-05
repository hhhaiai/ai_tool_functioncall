"""Semantic cache for the gateway.

Provides caching of responses based on semantic similarity of queries,
reducing upstream costs and latency for repeated or similar requests.

Now supports optional SQLite persistence via gateway_persistence module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from typing import Any, Optional

_logger = logging.getLogger(__name__)

Json = dict[str, Any]

# Optional persistence support - try both relative and absolute imports
_PERSISTENCE_AVAILABLE = False
try:
    from . import gateway_persistence as gp
    _PERSISTENCE_AVAILABLE = True
except ImportError:
    try:
        import gateway_persistence as gp
        _PERSISTENCE_AVAILABLE = True
    except ImportError:
        gp = None
        _logger.warning("gateway_persistence not available, caches will be memory-only")


class EmbeddingProvider:
    """Base class for embedding providers."""

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


class LocalEmbeddingProvider(EmbeddingProvider):
    """Simple local embedding using character-level features.

    This is a fallback when no external embedding service is available.
    It creates embeddings based on character n-grams and frequency analysis.
    """

    def __init__(self, dimension: int = 256):
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        """Generate a simple local embedding from text."""
        text = text.lower().strip()
        features = [0.0] * self.dimension

        # Character trigram features
        for i in range(len(text) - 2):
            trigram = text[i:i + 3]
            hash_val = int(hashlib.md5(trigram.encode()).hexdigest()[:8], 16)
            idx = hash_val % self.dimension
            features[idx] += 1.0

        # Word features
        words = text.split()
        for word in words:
            hash_val = int(hashlib.md5(word.encode()).hexdigest()[:8], 16)
            idx = hash_val % self.dimension
            features[idx] += 2.0

        # Normalize
        magnitude = math.sqrt(sum(f * f for f in features))
        if magnitude > 0:
            features = [f / magnitude for f in features]

        return features


class RemoteEmbeddingProvider(EmbeddingProvider):
    """Embedding provider that calls a remote service."""

    def __init__(self, url: str, model: str = "default", api_key: str = ""):
        self.url = url
        self.model = model
        self.api_key = api_key
        self._fallback = LocalEmbeddingProvider()

    def embed(self, text: str) -> list[float]:
        """Call remote embedding service."""
        import urllib.request
        import urllib.error

        payload = {
            "input": text,
            "model": self.model,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url,
            data=data,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                return result.get("data", [{}])[0].get("embedding", [])
        except Exception as exc:
            _logger.warning("Remote embedding failed, falling back to local: %s", exc)
            return self._fallback.embed(text)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    if len(a) != len(b):
        # Pad shorter vector
        max_len = max(len(a), len(b))
        a = a + [0.0] * (max_len - len(a))
        b = b + [0.0] * (max_len - len(b))

    dot_product = sum(x * y for x, y in zip(a, b))
    magnitude_a = math.sqrt(sum(x * x for x in a))
    magnitude_b = math.sqrt(sum(x * x for x in b))

    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0

    return dot_product / (magnitude_a * magnitude_b)


class CacheEntry:
    """A single cache entry."""

    def __init__(
        self,
        query: str,
        response: dict,
        embedding: list[float],
        ttl_seconds: int = 3600,
        scope_key: str = "",
    ):
        self.query = query
        self.response = response
        self.embedding = embedding
        self.scope_key = str(scope_key or "")
        self.created_at = time.time()
        self.last_accessed = self.created_at
        self.access_count = 0
        self.ttl_seconds = ttl_seconds

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def touch(self):
        """Update access time and count."""
        self.last_accessed = time.time()
        self.access_count += 1


class SemanticCache:
    """Semantic cache for query-response pairs.

    Stores responses indexed by semantic embeddings of queries,
    allowing cache hits for semantically similar requests.

    Now supports optional SQLite persistence.
    """

    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        max_entries: int = 1000,
        similarity_threshold: float = 0.92,
        ttl_seconds: int = 3600,
        enabled: bool = True,
        persistent: bool = False,
    ):
        self.embedding_provider = embedding_provider or LocalEmbeddingProvider()
        self.max_entries = max_entries
        self.similarity_threshold = similarity_threshold
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        self.persistent = persistent and _PERSISTENCE_AVAILABLE

        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._loaded_from_db = False

        # Load from database on initialization if persistent
        if self.persistent:
            self._load_from_db()

    def _load_from_db(self):
        """Load cache entries from database."""
        if not self.persistent or self._loaded_from_db:
            return

        try:
            entries = gp.load_semantic_cache_entries(max_age_seconds=self.ttl_seconds * 2)
            loaded_count = 0

            for entry_data in entries:
                # Reconstruct CacheEntry
                entry = CacheEntry(
                    query=entry_data["query"],
                    response=entry_data["response"],
                    embedding=entry_data["embedding"],
                    ttl_seconds=entry_data["ttl_seconds"],
                    scope_key=entry_data.get("scope_key", ""),
                )
                entry.created_at = entry_data["created_at"]
                entry.last_accessed = entry_data["last_accessed"]
                entry.access_count = entry_data["access_count"]

                # Skip expired entries
                if not entry.is_expired:
                    cache_key = entry_data["cache_key"]
                    self._entries[cache_key] = entry
                    loaded_count += 1

            self._loaded_from_db = True
            if loaded_count > 0:
                _logger.info(f"Loaded {loaded_count} semantic cache entries from database")

        except Exception as exc:
            _logger.error(f"Failed to load semantic cache from database: {exc}")

    def _save_to_db(self, cache_key: str, entry: CacheEntry):
        """Save a cache entry to database."""
        if not self.persistent:
            return

        try:
            gp.save_semantic_cache_entry(
                cache_key=cache_key,
                query=entry.query,
                embedding=entry.embedding,
                response=entry.response,
                ttl_seconds=entry.ttl_seconds,
                scope_key=entry.scope_key,
            )
        except Exception as exc:
            _logger.error(f"Failed to save semantic cache entry to database: {exc}")

    @property
    def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0.0,
                "similarity_threshold": self.similarity_threshold,
                "ttl_seconds": self.ttl_seconds,
            }

    def _normalize_scope_key(self, scope_key: str | None = None) -> str:
        return str(scope_key or "").strip()

    def _make_key(self, query: str, scope_key: str | None = None) -> str:
        """Create a cache key from query text."""
        normalized_query = query.lower().strip()
        normalized_scope = self._normalize_scope_key(scope_key)
        if not normalized_scope:
            return hashlib.sha256(normalized_query.encode()).hexdigest()[:32]
        payload = json.dumps(
            {"scope": normalized_scope, "query": normalized_query},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def _evict_expired(self):
        """Remove expired entries."""
        now = time.time()
        expired = [
            key for key, entry in self._entries.items()
            if entry.is_expired
        ]
        for key in expired:
            del self._entries[key]

    def _evict_lru(self):
        """Evict least recently used entries when over capacity."""
        if len(self._entries) <= self.max_entries:
            return

        # Sort by last accessed time
        sorted_entries = sorted(
            self._entries.items(),
            key=lambda x: x[1].last_accessed,
        )

        # Remove oldest entries until under capacity
        entries_to_remove = len(self._entries) - self.max_entries
        for key, _ in sorted_entries[:entries_to_remove]:
            del self._entries[key]

    def get(self, query: str, scope_key: str | None = None) -> Optional[dict]:
        """Get cached response for a query.

        Returns None if no sufficiently similar query is cached.
        """
        if not self.enabled:
            return None
        if not isinstance(query, str) or not query:
            return None

        normalized_scope = self._normalize_scope_key(scope_key)
        key = self._make_key(query, normalized_scope)

        # Fast path: exact match under lock (no embedding computation)
        with self._lock:
            self._evict_expired()
            if key in self._entries:
                entry = self._entries[key]
                if not entry.is_expired:
                    entry.touch()
                    self._hits += 1
                    # Update access stats in DB (non-blocking, best-effort)
                    if self.persistent:
                        try:
                            gp.touch_semantic_cache_entry(key)
                        except Exception:
                            pass
                    return entry.response

        # Slow path: compute embedding OUTSIDE the lock
        query_embedding = self.embedding_provider.embed(query)

        with self._lock:
            best_match = None
            best_similarity = 0.0

            for entry in self._entries.values():
                if entry.is_expired:
                    continue
                if entry.scope_key != normalized_scope:
                    continue

                similarity = cosine_similarity(query_embedding, entry.embedding)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = entry

            if best_match and best_similarity >= self.similarity_threshold:
                best_match.touch()
                self._hits += 1
                return best_match.response

            self._misses += 1
            return None

    def put(self, query: str, response: dict, scope_key: str | None = None) -> None:
        """Cache a response for a query."""
        if not self.enabled:
            return
        if not isinstance(query, str) or not query:
            return

        # Compute embedding OUTSIDE the lock
        normalized_scope = self._normalize_scope_key(scope_key)
        key = self._make_key(query, normalized_scope)
        embedding = self.embedding_provider.embed(query)

        with self._lock:
            self._evict_expired()

            entry = CacheEntry(
                query=query,
                response=response,
                embedding=embedding,
                ttl_seconds=self.ttl_seconds,
                scope_key=normalized_scope,
            )
            self._entries[key] = entry

            self._evict_lru()

        # Save to database OUTSIDE the lock
        if self.persistent:
            self._save_to_db(key, entry)

    def invalidate(self, pattern: str | None = None) -> int:
        """Invalidate cache entries matching a pattern.

        If pattern is None, invalidate all entries.
        Returns number of entries invalidated.
        """
        with self._lock:
            if pattern is None:
                count = len(self._entries)
                self._entries.clear()
                return count

            to_remove = []
            for key, entry in self._entries.items():
                if pattern in entry.query:
                    to_remove.append(key)

            for key in to_remove:
                del self._entries[key]

            return len(to_remove)

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0


class ToolResultCache:
    """Cache for deterministic tool results.

    Caches results of read-only tools (Read, Glob, Grep, etc.)
    by tool name and arguments hash.

    Now supports optional SQLite persistence.
    """

    # Tools that are safe to cache (deterministic, read-only)
    CACHEABLE_TOOLS = frozenset({
        "Read", "read_file",
        "Glob", "glob",
        "Grep", "grep",
        "FileInfo", "file_info",
        "LS", "list_directory",
        "Tree", "tree",
        "PythonSymbols", "python_symbols",
        "JsonQuery", "json_query",
        "WebFetch", "fetch_url",
        "WebSearch", "web_search",
    })

    def __init__(
        self,
        ttl_seconds: int = 30,
        max_entries: int = 500,
        persistent: bool = False,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.persistent = persistent and _PERSISTENCE_AVAILABLE
        self._cache: dict[str, tuple[float, str, str, dict]] = {}  # key -> (timestamp, result, tool_name, arguments)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _make_key(self, tool_name: str, arguments: dict) -> str:
        """Create cache key from tool name and arguments."""
        args_str = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        key_content = f"{tool_name}:{args_str}"
        return hashlib.sha256(key_content.encode()).hexdigest()[:32]

    def is_cacheable(self, tool_name: str) -> bool:
        """Check if a tool is safe to cache."""
        return tool_name in self.CACHEABLE_TOOLS

    def get(self, tool_name: str, arguments: dict) -> Optional[str]:
        """Get cached result for a tool call."""
        if not self.is_cacheable(tool_name):
            return None

        # Try persistent cache first if enabled
        if self.persistent:
            args_hash = self._make_key(tool_name, arguments)
            try:
                entry = gp.load_tool_cache_entry(tool_name, args_hash)
                if entry and entry["success"]:
                    self._hits += 1
                    return entry["result"]
            except Exception as exc:
                _logger.warning(f"Failed to load tool cache from database: {exc}")

        # Fall back to memory cache
        with self._lock:
            key = self._make_key(tool_name, arguments)
            if key in self._cache:
                timestamp, result, _, _ = self._cache[key]
                if time.time() - timestamp < self.ttl_seconds:
                    self._hits += 1
                    return result
                else:
                    del self._cache[key]

            self._misses += 1
            return None

    def put(self, tool_name: str, arguments: dict, result: str) -> None:
        """Cache a tool result."""
        if not self.is_cacheable(tool_name):
            return

        # Save to persistent cache if enabled
        if self.persistent:
            args_hash = self._make_key(tool_name, arguments)
            try:
                gp.save_tool_cache_entry(
                    tool_name=tool_name,
                    arguments_hash=args_hash,
                    result=result,
                    success=True,
                    ttl_seconds=self.ttl_seconds,
                )
            except Exception as exc:
                _logger.warning(f"Failed to save tool cache to database: {exc}")

        # Also save to memory cache for fast access
        with self._lock:
            key = self._make_key(tool_name, arguments)
            self._cache[key] = (time.time(), result, tool_name, arguments)

            # Evict old entries if over capacity
            if len(self._cache) > self.max_entries:
                # Remove oldest entries
                sorted_keys = sorted(
                    self._cache.keys(),
                    key=lambda k: self._cache[k][0],
                )
                for k in sorted_keys[:len(self._cache) - self.max_entries]:
                    del self._cache[k]

    def invalidate(self, tool_name: str | None = None, path: str | None = None) -> int:
        """Invalidate cache entries.

        Args:
            tool_name: If set, only invalidate entries for this tool.
            path: If set, only invalidate entries where path appears in arguments.

        Returns number of entries invalidated.
        """
        with self._lock:
            to_remove = []

            for key, (_, result, cached_tool_name, cached_args) in self._cache.items():
                should_remove = False

                if tool_name and cached_tool_name == tool_name:
                    should_remove = True

                if path:
                    # Check if path appears in any argument value
                    for arg_value in cached_args.values():
                        if isinstance(arg_value, str) and path in arg_value:
                            should_remove = True
                            break

                if should_remove:
                    to_remove.append(key)

            for key in to_remove:
                del self._cache[key]

            return len(to_remove)

    def clear(self) -> None:
        """Clear all cached results."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    @property
    def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            return {
                "entries": len(self._cache),
                "max_entries": self.max_entries,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0.0,
                "ttl_seconds": self.ttl_seconds,
            }


# Global cache instances
_semantic_cache: Optional[SemanticCache] = None
_tool_result_cache: Optional[ToolResultCache] = None
_cache_lock = threading.Lock()


def get_semantic_cache() -> SemanticCache:
    """Get or create the global semantic cache instance."""
    global _semantic_cache
    if _semantic_cache is None:
        with _cache_lock:
            if _semantic_cache is None:
                from .gateway_config import load_config
                config = load_config()
                cache_config = config.get("cache", {})
                persistence_config = config.get("persistence", {})

                # Determine embedding provider
                embedding_url = cache_config.get("embedding_url", "")
                if embedding_url:
                    provider = RemoteEmbeddingProvider(
                        url=embedding_url,
                        model=cache_config.get("embedding_model", "default"),
                        api_key=cache_config.get("embedding_api_key", ""),
                    )
                    default_threshold = 0.92
                else:
                    provider = LocalEmbeddingProvider()
                    # Lower threshold for local (crude) embeddings
                    default_threshold = 0.75

                _semantic_cache = SemanticCache(
                    embedding_provider=provider,
                    max_entries=cache_config.get("max_entries", 1000),
                    similarity_threshold=cache_config.get("similarity_threshold", default_threshold),
                    ttl_seconds=cache_config.get("ttl_seconds", 3600),
                    enabled=cache_config.get("enabled", True),
                    persistent=persistence_config.get("enabled", True),
                )

    return _semantic_cache


def get_tool_result_cache() -> ToolResultCache:
    """Get or create the global tool result cache instance."""
    global _tool_result_cache
    if _tool_result_cache is None:
        with _cache_lock:
            if _tool_result_cache is None:
                from .gateway_config import load_config
                config = load_config()
                persistence_config = config.get("persistence", {})

                _tool_result_cache = ToolResultCache(
                    ttl_seconds=30,
                    max_entries=500,
                    persistent=persistence_config.get("enabled", True),
                )

    return _tool_result_cache


def reset_caches() -> None:
    """Reset all caches (for testing)."""
    global _semantic_cache, _tool_result_cache
    with _cache_lock:
        _semantic_cache = None
        _tool_result_cache = None
