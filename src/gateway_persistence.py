#!/usr/bin/env python3
"""Persistence layer for the gateway.

Provides SQLite-based persistence for:
- Semantic cache entries
- Tool result cache
- Statistics (already in gateway_stats.py, will unify)
- Memory system (conversation memories)

This module centralizes all database operations and provides
a clean API for other modules to persist data.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    from .gateway_sqlite import path_is_within, secure_sqlite_artifacts, secure_sqlite_connect, set_secure_sqlite_journal_mode
except ImportError:  # pragma: no cover - legacy top-level import mode
    from gateway_sqlite import path_is_within, secure_sqlite_artifacts, secure_sqlite_connect, set_secure_sqlite_journal_mode

_logger = logging.getLogger(__name__)

Json = dict[str, Any]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PersistenceConfig:
    """Configuration for persistence layer."""
    enabled: bool = True
    db_path: str = ".gateway_runtime/gateway.db"
    auto_vacuum: bool = True
    cache_size_kb: int = 10000  # 10MB cache
    journal_mode: str = "WAL"  # Write-Ahead Logging
    synchronous: str = "NORMAL"  # Balance between safety and speed


def _persistence_config(raw: dict | None = None) -> PersistenceConfig:
    """Parse persistence config from raw dict."""
    if not raw:
        return PersistenceConfig()
    return PersistenceConfig(
        enabled=raw.get("enabled", True),
        db_path=raw.get("db_path", ".gateway_runtime/gateway.db"),
        auto_vacuum=raw.get("auto_vacuum", True),
        cache_size_kb=raw.get("cache_size_kb", 10000),
        journal_mode=raw.get("journal_mode", "WAL"),
        synchronous=raw.get("synchronous", "NORMAL"),
    )


# ---------------------------------------------------------------------------
# Database Connection Management
# ---------------------------------------------------------------------------

_db_lock = threading.RLock()
_db_conn: sqlite3.Connection | None = None
_db_path: str | None = None


def _get_db(config: PersistenceConfig | None = None) -> sqlite3.Connection:
    """Get or create database connection.

    Args:
        config: Optional config override. If not provided, uses existing connection.

    Returns:
        SQLite connection object.
    """
    global _db_conn, _db_path

    if config is None:
        # Return existing connection
        if _db_conn is None:
            raise RuntimeError("Database not initialized. Call init_persistence() first.")
        return _db_conn

    # Initialize or reinitialize
    target_path = config.db_path if config.enabled else ":memory:"

    if _db_conn is not None and _db_path == target_path:
        # Already connected to correct database
        return _db_conn

    with _db_lock:
        # Close existing connection if switching databases
        if _db_conn is not None:
            _db_conn.close()
            _db_conn = None

        # Create connection
        if target_path == ":memory:":
            _db_conn = sqlite3.connect(
                target_path,
                check_same_thread=False,
                timeout=30.0,
            )
        else:
            runtime_dir = Path(os.environ.get("GATEWAY_RUNTIME_DIR") or ".gateway_runtime")
            _db_conn = secure_sqlite_connect(
                target_path,
                private_parent=path_is_within(target_path, runtime_dir),
                check_same_thread=False,
                timeout=30.0,
            )
        _db_conn.row_factory = sqlite3.Row
        _db_path = target_path

        # Configure connection
        cursor = _db_conn.cursor()
        if config.auto_vacuum:
            cursor.execute("PRAGMA auto_vacuum = INCREMENTAL")
        if target_path == ":memory:":
            _db_conn.execute("PRAGMA journal_mode=MEMORY")
        else:
            set_secure_sqlite_journal_mode(_db_conn, target_path, config.journal_mode)
        if target_path != ":memory:":
            secure_sqlite_artifacts(target_path)
        cursor.execute(f"PRAGMA synchronous = {config.synchronous}")
        cursor.execute(f"PRAGMA cache_size = -{config.cache_size_kb}")
        cursor.close()

        # Initialize schema
        _init_schema(_db_conn)

        _logger.info(f"Database initialized: {target_path}")

    return _db_conn


def init_persistence(config: PersistenceConfig | None = None):
    """Initialize persistence layer.

    This should be called once at application startup.

    Args:
        config: Optional persistence configuration. Uses defaults if not provided.
    """
    if config is None:
        config = PersistenceConfig()
    _get_db(config)


def close_persistence():
    """Close database connection gracefully."""
    global _db_conn
    with _db_lock:
        if _db_conn is not None:
            _db_conn.close()
            _db_conn = None
            _logger.info("Database connection closed")


# ---------------------------------------------------------------------------
# Schema Management
# ---------------------------------------------------------------------------

def _init_schema(conn: sqlite3.Connection):
    """Initialize or migrate database schema."""
    cursor = conn.cursor()

    # Check schema version
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        )
    """)

    cursor.execute("SELECT MAX(version) FROM schema_version")
    current_version = cursor.fetchone()[0] or 0

    # Apply migrations
    migrations = [
        _migration_v1_semantic_cache,
        _migration_v2_tool_cache,
        _migration_v3_memories,
        _migration_v4_semantic_cache_scope,
        _migration_v5_tool_cache_scope,
    ]

    for version, migration in enumerate(migrations, start=1):
        if version > current_version:
            _logger.info(f"Applying migration v{version}...")
            migration(cursor)
            cursor.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, time.time())
            )
            conn.commit()

    cursor.close()


def _migration_v1_semantic_cache(cursor: sqlite3.Cursor):
    """Migration v1: Semantic cache tables."""
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS semantic_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT NOT NULL UNIQUE,
            query TEXT NOT NULL,
            embedding BLOB NOT NULL,
            response TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_accessed REAL NOT NULL,
            access_count INTEGER DEFAULT 0,
            ttl_seconds INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_cache_created
            ON semantic_cache(created_at);
        CREATE INDEX IF NOT EXISTS idx_semantic_cache_accessed
            ON semantic_cache(last_accessed);
    """)


def _migration_v2_tool_cache(cursor: sqlite3.Cursor):
    """Migration v2: Tool result cache tables."""
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS tool_cache (
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
        );

        CREATE INDEX IF NOT EXISTS idx_tool_cache_tool
            ON tool_cache(tool_name);
        CREATE INDEX IF NOT EXISTS idx_tool_cache_created
            ON tool_cache(created_at);
    """)


def _migration_v5_tool_cache_scope(cursor: sqlite3.Cursor):
    """Add queryable tenant/workspace scope to persistent tool cache.

    Existing rows were keyed with scope data only inside an opaque hash, so
    they cannot be invalidated safely after a workspace mutation.  Drop those
    short-lived legacy rows during migration rather than allowing stale values
    to survive an upgrade.
    """
    columns = {
        str(row[1])
        for row in cursor.execute("PRAGMA table_info(tool_cache)").fetchall()
    }
    if "workspace_key" not in columns:
        cursor.execute("ALTER TABLE tool_cache ADD COLUMN workspace_key TEXT NOT NULL DEFAULT ''")
    if "runtime_key" not in columns:
        cursor.execute("ALTER TABLE tool_cache ADD COLUMN runtime_key TEXT NOT NULL DEFAULT ''")
    cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_tool_cache_scope
            ON tool_cache(workspace_key, runtime_key);
        DELETE FROM tool_cache;
    """)


def _migration_v3_memories(cursor: sqlite3.Cursor):
    """Migration v3: Memory system tables."""
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            embedding BLOB,
            importance REAL DEFAULT 0.5,
            created_at REAL NOT NULL,
            last_accessed REAL NOT NULL,
            access_count INTEGER DEFAULT 0,
            tags TEXT,
            metadata TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_memories_importance
            ON memories(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_created
            ON memories(created_at DESC);
    """)


def _migration_v4_semantic_cache_scope(cursor: sqlite3.Cursor):
    """Migration v4: Semantic cache tenant/runtime scope."""
    cursor.execute("PRAGMA table_info(semantic_cache)")
    existing_columns = {str(row[1]) for row in cursor.fetchall()}
    if "scope_key" not in existing_columns:
        cursor.execute("ALTER TABLE semantic_cache ADD COLUMN scope_key TEXT DEFAULT ''")
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_semantic_cache_scope
            ON semantic_cache(scope_key);
    """)


# ---------------------------------------------------------------------------
# Semantic Cache Persistence
# ---------------------------------------------------------------------------

def save_semantic_cache_entry(
    cache_key: str,
    query: str,
    embedding: list[float],
    response: dict,
    ttl_seconds: int,
    scope_key: str = "",
) -> bool:
    """Save a semantic cache entry to database.

    Args:
        cache_key: Unique cache key (hash of query)
        query: Original query text
        embedding: Query embedding vector
        response: Cached response
        ttl_seconds: Time-to-live in seconds

    Returns:
        True if saved successfully, False otherwise
    """
    try:
        conn = _get_db()
        now = time.time()

        # Serialize embedding and response
        embedding_blob = json.dumps(embedding).encode()
        response_json = json.dumps(response)

        conn.execute("""
            INSERT OR REPLACE INTO semantic_cache
                (cache_key, query, embedding, response, created_at, last_accessed, access_count, ttl_seconds, scope_key)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (cache_key, query, embedding_blob, response_json, now, now, ttl_seconds, str(scope_key or "")))

        conn.commit()
        return True

    except Exception as exc:
        _logger.error(f"Failed to save semantic cache entry: {exc}")
        return False


def load_semantic_cache_entries(max_age_seconds: int | None = None) -> list[dict]:
    """Load semantic cache entries from database.

    Args:
        max_age_seconds: Only load entries newer than this age. None = load all.

    Returns:
        List of cache entry dicts with keys: cache_key, query, embedding, response, etc.
    """
    try:
        conn = _get_db()
        cursor = conn.cursor()

        if max_age_seconds is not None:
            cutoff = time.time() - max_age_seconds
            cursor.execute("""
                SELECT cache_key, query, embedding, response, created_at, last_accessed, access_count, ttl_seconds, scope_key
                FROM semantic_cache
                WHERE created_at > ?
                ORDER BY created_at DESC
            """, (cutoff,))
        else:
            cursor.execute("""
                SELECT cache_key, query, embedding, response, created_at, last_accessed, access_count, ttl_seconds, scope_key
                FROM semantic_cache
                ORDER BY created_at DESC
            """)

        entries = []
        for row in cursor.fetchall():
            entries.append({
                "cache_key": row[0],
                "query": row[1],
                "embedding": json.loads(row[2]),
                "response": json.loads(row[3]),
                "created_at": row[4],
                "last_accessed": row[5],
                "access_count": row[6],
                "ttl_seconds": row[7],
                "scope_key": row[8] or "",
            })

        cursor.close()
        return entries

    except Exception as exc:
        _logger.error(f"Failed to load semantic cache entries: {exc}")
        return []


def touch_semantic_cache_entry(cache_key: str) -> bool:
    """Update last_accessed time and increment access_count for a cache entry.

    Args:
        cache_key: Cache key to update

    Returns:
        True if updated successfully, False otherwise
    """
    try:
        conn = _get_db()
        now = time.time()

        conn.execute("""
            UPDATE semantic_cache
            SET last_accessed = ?, access_count = access_count + 1
            WHERE cache_key = ?
        """, (now, cache_key))

        conn.commit()
        return True

    except Exception as exc:
        _logger.error(f"Failed to touch semantic cache entry: {exc}")
        return False


def delete_semantic_cache_entry(cache_key: str) -> bool:
    """Delete a semantic cache entry.

    Args:
        cache_key: Cache key to delete

    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        conn = _get_db()
        conn.execute("DELETE FROM semantic_cache WHERE cache_key = ?", (cache_key,))
        conn.commit()
        return True

    except Exception as exc:
        _logger.error(f"Failed to delete semantic cache entry: {exc}")
        return False


def cleanup_expired_semantic_cache(
    ttl_buffer: float = 0.0,
    *,
    batch_size: int = 1_000,
    max_batches: int = 4,
    strict: bool = False,
) -> int:
    """Delete expired semantic cache entries.

    Args:
        ttl_buffer: Extra time (seconds) to keep entries beyond their TTL

    Returns:
        Number of entries deleted
    """
    try:
        conn = _get_db()
        now = time.time()

        deleted = 0
        batch_size = max(1, min(int(batch_size), 100_000))
        for _ in range(max(1, min(int(max_batches), 100))):
            result = conn.execute("""
                DELETE FROM semantic_cache WHERE cache_key IN (
                    SELECT cache_key FROM semantic_cache
                    WHERE (created_at + ttl_seconds + ?) < ?
                    ORDER BY created_at LIMIT ?
                )
            """, (ttl_buffer, now, batch_size))
            changed = max(0, int(result.rowcount))
            deleted += changed
            conn.commit()
            if changed < batch_size:
                break

        if deleted > 0:
            _logger.info(f"Cleaned up {deleted} expired semantic cache entries")

        return deleted

    except Exception as exc:
        _logger.error(f"Failed to cleanup semantic cache: {exc}")
        if strict:
            raise
        return 0


# ---------------------------------------------------------------------------
# Tool Cache Persistence
# ---------------------------------------------------------------------------

def save_tool_cache_entry(
    tool_name: str,
    arguments_hash: str,
    result: str,
    success: bool,
    ttl_seconds: int,
    workspace_key: str = "",
    runtime_key: str = "",
) -> bool:
    """Save a tool result cache entry to database.

    Args:
        tool_name: Name of the tool
        arguments_hash: Hash of tool arguments
        result: Tool execution result
        success: Whether execution succeeded
        ttl_seconds: Time-to-live in seconds

    Returns:
        True if saved successfully, False otherwise
    """
    try:
        conn = _get_db()
        now = time.time()
        cache_key = f"{tool_name}:{arguments_hash}"

        conn.execute("""
            INSERT OR REPLACE INTO tool_cache
                (cache_key, tool_name, arguments_hash, result, success, created_at, last_accessed,
                 access_count, ttl_seconds, workspace_key, runtime_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """, (
            cache_key,
            tool_name,
            arguments_hash,
            result,
            success,
            now,
            now,
            ttl_seconds,
            str(workspace_key or ""),
            str(runtime_key or ""),
        ))

        conn.commit()
        return True

    except RuntimeError as exc:
        if "Database not initialized" in str(exc):
            return False
        _logger.error(f"Failed to save tool cache entry: {exc}")
        return False
    except Exception as exc:
        _logger.error(f"Failed to save tool cache entry: {exc}")
        return False


def load_tool_cache_entry(tool_name: str, arguments_hash: str) -> dict | None:
    """Load a tool cache entry from database.

    Args:
        tool_name: Name of the tool
        arguments_hash: Hash of tool arguments

    Returns:
        Cache entry dict or None if not found/expired
    """
    try:
        conn = _get_db()
        cache_key = f"{tool_name}:{arguments_hash}"
        now = time.time()

        cursor = conn.cursor()
        cursor.execute("""
            SELECT result, success, created_at, ttl_seconds, access_count
            FROM tool_cache
            WHERE cache_key = ?
        """, (cache_key,))

        row = cursor.fetchone()
        cursor.close()

        if row is None:
            return None

        result, success, created_at, ttl_seconds, access_count = row

        # Check expiration
        if (now - created_at) > ttl_seconds:
            # Expired, delete it
            conn.execute("DELETE FROM tool_cache WHERE cache_key = ?", (cache_key,))
            conn.commit()
            return None

        # Update access stats (in separate transaction for performance)
        try:
            conn.execute("""
                UPDATE tool_cache
                SET last_accessed = ?, access_count = access_count + 1
                WHERE cache_key = ?
            """, (now, cache_key))
            conn.commit()
        except Exception:
            pass  # Non-critical

        return {
            "result": result,
            "success": success,
            "created_at": created_at,
            "access_count": access_count + 1,
        }

    except RuntimeError as exc:
        if "Database not initialized" in str(exc):
            return None
        _logger.error(f"Failed to load tool cache entry: {exc}")
        return None
    except Exception as exc:
        _logger.error(f"Failed to load tool cache entry: {exc}")
        return None


def cleanup_expired_tool_cache(
    *,
    batch_size: int = 1_000,
    max_batches: int = 4,
    strict: bool = False,
) -> int:
    """Delete expired tool cache entries.

    Returns:
        Number of entries deleted
    """
    try:
        conn = _get_db()
        now = time.time()

        deleted = 0
        batch_size = max(1, min(int(batch_size), 100_000))
        for _ in range(max(1, min(int(max_batches), 100))):
            result = conn.execute("""
                DELETE FROM tool_cache WHERE cache_key IN (
                    SELECT cache_key FROM tool_cache
                    WHERE (created_at + ttl_seconds) < ?
                    ORDER BY created_at LIMIT ?
                )
            """, (now, batch_size))
            changed = max(0, int(result.rowcount))
            deleted += changed
            conn.commit()
            if changed < batch_size:
                break

        if deleted > 0:
            _logger.info(f"Cleaned up {deleted} expired tool cache entries")

        return deleted

    except Exception as exc:
        _logger.error(f"Failed to cleanup tool cache: {exc}")
        if strict:
            raise
        return 0


def delete_tool_cache_scope(workspace_key: str, runtime_key: str) -> int:
    """Delete persistent tool-cache rows for one exact runtime scope."""
    if not workspace_key or not runtime_key:
        return 0
    try:
        conn = _get_db()
        result = conn.execute(
            "DELETE FROM tool_cache WHERE workspace_key = ? AND runtime_key = ?",
            (str(workspace_key), str(runtime_key)),
        )
        deleted = max(0, int(result.rowcount or 0))
        conn.commit()
        return deleted
    except RuntimeError as exc:
        if "Database not initialized" in str(exc):
            return 0
        _logger.error(f"Failed to invalidate tool cache scope: {exc}")
        return 0
    except Exception as exc:
        _logger.error(f"Failed to invalidate tool cache scope: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Memory System Persistence
# ---------------------------------------------------------------------------

def save_memory(
    memory_id: str,
    content: str,
    embedding: list[float] | None = None,
    importance: float = 0.5,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> bool:
    """Save a memory entry to database.

    Args:
        memory_id: Unique memory identifier
        content: Memory content text
        embedding: Optional embedding vector
        importance: Importance score (0-1)
        tags: Optional list of tags
        metadata: Optional metadata dict

    Returns:
        True if saved successfully, False otherwise
    """
    try:
        conn = _get_db()
        now = time.time()

        embedding_blob = json.dumps(embedding).encode() if embedding else None
        tags_json = json.dumps(tags) if tags else None
        metadata_json = json.dumps(metadata) if metadata else None

        conn.execute("""
            INSERT OR REPLACE INTO memories
                (memory_id, content, embedding, importance, created_at, last_accessed, access_count, tags, metadata)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (memory_id, content, embedding_blob, importance, now, now, tags_json, metadata_json))

        conn.commit()
        return True

    except Exception as exc:
        _logger.error(f"Failed to save memory: {exc}")
        return False


def load_memories(limit: int | None = None, min_importance: float = 0.0) -> list[dict]:
    """Load memories from database.

    Args:
        limit: Maximum number of memories to load
        min_importance: Minimum importance threshold

    Returns:
        List of memory dicts ordered by importance and recency
    """
    try:
        conn = _get_db()
        cursor = conn.cursor()

        query = """
            SELECT memory_id, content, embedding, importance, created_at, last_accessed, access_count, tags, metadata
            FROM memories
            WHERE importance >= ?
            ORDER BY importance DESC, created_at DESC
        """

        if limit:
            query += f" LIMIT {limit}"

        cursor.execute(query, (min_importance,))

        memories = []
        for row in cursor.fetchall():
            memories.append({
                "memory_id": row[0],
                "content": row[1],
                "embedding": json.loads(row[2]) if row[2] else None,
                "importance": row[3],
                "created_at": row[4],
                "last_accessed": row[5],
                "access_count": row[6],
                "tags": json.loads(row[7]) if row[7] else [],
                "metadata": json.loads(row[8]) if row[8] else {},
            })

        cursor.close()
        return memories

    except Exception as exc:
        _logger.error(f"Failed to load memories: {exc}")
        return []


def search_memories(query_embedding: list[float], top_k: int = 5) -> list[dict]:
    """Search memories by embedding similarity.

    Note: This is a simple linear scan. For production, consider using
    a vector database like Faiss, Pinecone, or Qdrant.

    Args:
        query_embedding: Query embedding vector
        top_k: Number of top results to return

    Returns:
        List of memory dicts with similarity scores
    """
    try:
        conn = _get_db()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT memory_id, content, embedding, importance, created_at, last_accessed, access_count
            FROM memories
            WHERE embedding IS NOT NULL
        """)

        results = []
        for row in cursor.fetchall():
            memory_embedding = json.loads(row[2])

            # Calculate cosine similarity
            from gateway_cache import cosine_similarity
            similarity = cosine_similarity(query_embedding, memory_embedding)

            results.append({
                "memory_id": row[0],
                "content": row[1],
                "importance": row[3],
                "created_at": row[4],
                "access_count": row[6],
                "similarity": similarity,
            })

        cursor.close()

        # Sort by similarity and return top_k
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]

    except Exception as exc:
        _logger.error(f"Failed to search memories: {exc}")
        return []


def delete_memory(memory_id: str) -> bool:
    """Delete a memory entry.

    Args:
        memory_id: Memory ID to delete

    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        conn = _get_db()
        conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
        conn.commit()
        return True

    except Exception as exc:
        _logger.error(f"Failed to delete memory: {exc}")
        return False


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def vacuum_database():
    """Run VACUUM to reclaim space and optimize database."""
    try:
        conn = _get_db()
        conn.execute("VACUUM")
        _logger.info("Database vacuumed successfully")
    except Exception as exc:
        _logger.error(f"Failed to vacuum database: {exc}")


def maintain_database(*, incremental_vacuum_pages: int = 256) -> dict:
    """Checkpoint WAL and incrementally reclaim free pages with observable errors."""
    conn = _get_db()
    checkpoint = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    pages = max(0, min(int(incremental_vacuum_pages), 100_000))
    if pages:
        conn.execute(f"PRAGMA incremental_vacuum({pages})")
    if _db_path and _db_path != ":memory:":
        secure_sqlite_artifacts(_db_path)
    return {
        "auto_vacuum_mode": int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]),
        "checkpoint": {
            "busy": int(checkpoint[0]),
            "log_frames": int(checkpoint[1]),
            "checkpointed_frames": int(checkpoint[2]),
        },
        "space_bytes": sum(
            os.path.getsize(candidate)
            for candidate in (_db_path, f"{_db_path}-wal", f"{_db_path}-shm")
            if _db_path and _db_path != ":memory:" and os.path.exists(candidate)
        ),
    }


def get_database_stats() -> dict:
    """Get database statistics.

    Returns:
        Dict with stats: db_path, size_bytes, semantic_cache_entries, tool_cache_entries, memories
    """
    try:
        conn = _get_db()
        cursor = conn.cursor()

        stats = {
            "db_path": _db_path or "unknown",
            "size_bytes": 0,
            "semantic_cache_entries": 0,
            "tool_cache_entries": 0,
            "memories": 0,
        }

        # Get database size
        if _db_path and _db_path != ":memory:":
            try:
                stats["size_bytes"] = os.path.getsize(_db_path)
            except Exception:
                pass

        # Get table counts
        cursor.execute("SELECT COUNT(*) FROM semantic_cache")
        stats["semantic_cache_entries"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM tool_cache")
        stats["tool_cache_entries"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM memories")
        stats["memories"] = cursor.fetchone()[0]

        cursor.close()
        return stats

    except Exception as exc:
        _logger.error(f"Failed to get database stats: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "PersistenceConfig",
    "init_persistence",
    "close_persistence",
    "save_semantic_cache_entry",
    "load_semantic_cache_entries",
    "touch_semantic_cache_entry",
    "delete_semantic_cache_entry",
    "cleanup_expired_semantic_cache",
    "save_tool_cache_entry",
    "load_tool_cache_entry",
    "delete_tool_cache_scope",
    "cleanup_expired_tool_cache",
    "save_memory",
    "load_memories",
    "search_memories",
    "delete_memory",
    "vacuum_database",
    "maintain_database",
    "get_database_stats",
]
