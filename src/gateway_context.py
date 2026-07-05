#!/usr/bin/env python3
"""Context management for the gateway.

Handles context compaction, memory system, and fanout for long conversations.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import uuid
from collections import OrderedDict
from typing import Any

Json = dict[str, Any]

# Thread-safe, bounded summary cache (LRU eviction)
_SUMMARY_CACHE_MAX = 512
_summary_cache_lock = threading.Lock()
_SUMMARY_CACHE: OrderedDict[str, str] = OrderedDict()


def _summary_cache_get(key: str) -> str | None:
    with _summary_cache_lock:
        if key in _SUMMARY_CACHE:
            _SUMMARY_CACHE.move_to_end(key)
            return _SUMMARY_CACHE[key]
    return None


def _summary_cache_put(key: str, value: str) -> None:
    with _summary_cache_lock:
        if key in _SUMMARY_CACHE:
            _SUMMARY_CACHE.move_to_end(key)
        _SUMMARY_CACHE[key] = value
        while len(_SUMMARY_CACHE) > _SUMMARY_CACHE_MAX:
            _SUMMARY_CACHE.popitem(last=False)


def _approx_token_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        # Conservative enough for routing decisions without adding tokenizer deps:
        # ASCII-ish text averages ~4 chars/token; CJK is closer to 1-2 chars/token.
        cjk = sum(1 for ch in value if "一" <= ch <= "鿿")
        other = max(len(value) - cjk, 0)
        return cjk + max(1, other // 4)
    if isinstance(value, (int, float, bool)):
        return 1
    if isinstance(value, dict):
        value = value.get("content") or value.get("text") or json.dumps(value, ensure_ascii=False)
        return _approx_token_count(value)
    if isinstance(value, list):
        return sum(_approx_token_count(item) for item in value)
    return 0


def _context_config() -> Json:
    from .gateway_config import load_config
    return load_config().get("context", {})


def _context_enabled() -> bool:
    cfg = _context_config()
    # Default to enabled unless explicitly disabled
    return cfg.get("enabled", True)


def _body_token_estimate(body: Json) -> int:
    body_without_tools = {k: v for k, v in body.items() if k not in {"tools", "tool_choice"}}
    return _approx_token_count(body_without_tools)


def _gateway_system_prompt(reason: str = "context_compaction") -> str:
    return (
        "[gateway context compacted]\n"
        "[Gateway context management: This conversation has been compacted to fit within "
        "the context window. Recent messages have been preserved, and older messages have "
        "been summarized. Tool call results from earlier in the conversation may have been "
        "truncated or summarized.]\n\n"
    )


def _content_contains_gateway_prompt(value: Any) -> bool:
    text = ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                text += str(item.get("text") or "")
    if "[Gateway context management:" in text:
        return True
    return False


def _inject_gateway_system_prompt(path: str, body: Json, *, reason: str) -> Json:
    from .gateway_protocol import _text_from_content
    body = __import__("copy").deepcopy(body)
    messages = body.get("messages") or []
    if not messages:
        return body
    gateway_prompt = _gateway_system_prompt(reason)
    if "/messages" in path:
        first_content = messages[0].get("content") if isinstance(messages[0], dict) else ""
        if _content_contains_gateway_prompt(first_content):
            return body
        if isinstance(messages[0], dict) and messages[0].get("role") == "user":
            if isinstance(messages[0].get("content"), list):
                messages[0]["content"].insert(0, {"type": "text", "text": gateway_prompt})
            elif isinstance(messages[0].get("content"), str):
                messages[0]["content"] = [{"type": "text", "text": gateway_prompt + messages[0]["content"]}]
    else:
        first_content = messages[0].get("content") if isinstance(messages[0], dict) else ""
        if _content_contains_gateway_prompt(first_content):
            return body
        if isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages[0]["content"] = gateway_prompt + str(messages[0].get("content") or "")
        else:
            messages.insert(0, {"role": "system", "content": gateway_prompt})
    body["messages"] = messages
    return body


# Memory system
def _memory_config() -> Json:
    return _context_config()


def _memory_enabled() -> bool:
    cfg = _memory_config()
    return cfg.get("memory_enabled", True)


def _json_object_from_maybe_string(value: Any) -> Json:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _stable_memory_key_part(value: Any, *, max_len: int = 96) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(text or "").strip()
    if not text:
        return ""
    if len(text) <= max_len and re.fullmatch(r"[A-Za-z0-9_.:@+-]+", text):
        return text
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:24]


def _tenant_key_from_body(body: Json, metadata: Json | None = None) -> str:
    metadata = metadata if isinstance(metadata, dict) else body.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = _json_object_from_maybe_string(metadata)
    user_meta = _json_object_from_maybe_string(metadata.get("user_id"))
    scoped_client_id = None
    try:
        from . import gateway_builtin_tools as _bt
        scoped_client_id = _bt._CLIENT_ID_SCOPE_OVERRIDE.get()
    except Exception:
        scoped_client_id = None
    candidates = (
        metadata.get("tenant"),
        metadata.get("tenant_id"),
        metadata.get("account_id"),
        metadata.get("organization_id"),
        user_meta.get("tenant"),
        user_meta.get("tenant_id"),
        user_meta.get("account_id"),
        user_meta.get("organization_id"),
        user_meta.get("user_id"),
        user_meta.get("user"),
        user_meta.get("email"),
        metadata.get("user_id"),
        metadata.get("user"),
        body.get("user"),
        scoped_client_id,
        body.get("client_id"),
    )
    for candidate in candidates:
        part = _stable_memory_key_part(candidate)
        if part:
            return part
    return "anonymous"


def _memory_session_key(body: Json) -> str:
    metadata = body.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = _json_object_from_maybe_string(metadata)
    tenant = _tenant_key_from_body(body, metadata)
    session_id = metadata.get("session_id") or metadata.get("conversation_id") or ""
    if not session_id:
        user_meta = _json_object_from_maybe_string(metadata.get("user_id"))
        session_id = user_meta.get("session_id") or user_meta.get("conversation_id") or ""
    if session_id:
        return f"tenant:{tenant}:session:{_stable_memory_key_part(session_id)}"
    messages = body.get("messages") or []
    if tenant != "anonymous" and messages:
        first_msg = messages[0]
        if isinstance(first_msg, dict):
            content = first_msg.get("content") or ""
            if isinstance(content, str) and len(content) > 10:
                digest = hashlib.sha256(content[:100].encode()).hexdigest()[:16]
                return f"tenant:{tenant}:anon:{digest}"
    return f"tenant:{tenant}:session_{uuid.uuid4().hex[:12]}"


def _memory_workspace_root(body: Json | None = None) -> str:
    try:
        from . import gateway_builtin_tools as _bt
        override = _bt._WORKSPACE_ROOT_OVERRIDE.get()
        if override is not None:
            return str(pathlib.Path(override).resolve())
    except Exception:
        pass
    if isinstance(body, dict):
        try:
            from .gateway_tool_runtime import (
                _body_has_client_workspace_hint,
                _body_has_remote_identity,
                _has_non_absolute_client_workspace_hint,
                _request_workspace_root,
            )
            if (
                _body_has_client_workspace_hint(body)
                or _has_non_absolute_client_workspace_hint(body)
                or _body_has_remote_identity(body)
            ):
                return str(_request_workspace_root(body))
        except Exception:
            pass
    return "default"


def _memory_workspace_legacy_hash(workspace: str) -> str:
    return __import__("hashlib").sha256(workspace.encode()).hexdigest()[:16]


def _memory_workspace_key(body: Json | None = None) -> str:
    # Store the resolved downstream project root, not a service-root hash, so
    # Memory/RecallMemory evidence remains auditable and project-scoped.
    return _memory_workspace_root(body)


def _memory_workspace_lookup_keys(workspace: str) -> list[str]:
    legacy = _memory_workspace_legacy_hash(workspace)
    return [workspace] if legacy == workspace else [workspace, legacy]


def _memory_session_index_parts(session_key: str) -> Json:
    text = str(session_key or "")
    tenant = "anonymous"
    memory_session = text
    if text.startswith("tenant:"):
        rest = text[len("tenant:"):]
        markers = (":session:", ":conversation:", ":thread:", ":anon:", ":session_")
        marker_positions = [(rest.find(marker), marker) for marker in markers if rest.find(marker) >= 0]
        if marker_positions:
            pos, marker = min(marker_positions, key=lambda item: item[0])
            tenant = rest[:pos] or "anonymous"
            suffix = rest[pos + len(marker):]
            memory_session = marker.strip(":_") + ":" + suffix
        else:
            tenant = rest or "anonymous"
            memory_session = ""
    return {"tenant_key": tenant, "memory_session_key": memory_session}


def _memory_index_fields(session_key: str, workspace_root: str) -> Json:
    parts = _memory_session_index_parts(session_key)
    return {
        "tenant_key": str(parts.get("tenant_key") or "anonymous"),
        "workspace_key": str(workspace_root or "default"),
        "memory_session_key": str(parts.get("memory_session_key") or session_key or ""),
    }


def _sqlite_backfill_memory_index_fields(conn: Any, *, limit: int = 5000) -> None:
    try:
        rows = conn.execute(
            """
            SELECT id, session_key, workspace_root
            FROM conversation_memories
            WHERE tenant_key='' OR workspace_key='' OR memory_session_key=''
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception:
        return
    updated = False
    for row in rows:
        fields = _memory_index_fields(str(row[1] or ""), str(row[2] or ""))
        conn.execute(
            """
            UPDATE conversation_memories
            SET tenant_key=?, workspace_key=?, memory_session_key=?
            WHERE id=?
            """,
            (fields["tenant_key"], fields["workspace_key"], fields["memory_session_key"], row[0]),
        )
        updated = True
    if updated:
        conn.commit()


def _memory_extract_keywords(text: str, *, limit: int = 40) -> list[str]:
    words = re.findall(r'\b[a-zA-Z_]\w{2,}\b', text.lower())
    seen: set[str] = set()
    keywords = []
    for w in words:
        if w not in seen and w not in {"the", "and", "for", "that", "this", "with", "from", "are", "was", "were", "been", "have", "has", "had", "will", "would", "could", "should", "may", "might", "can", "shall", "not", "but", "what", "which", "who", "when", "where", "how", "why", "all", "each", "every", "both", "few", "more", "most", "other", "some", "such", "than", "too", "very", "just", "about"}:
            seen.add(w)
            keywords.append(w)
            if len(keywords) >= limit:
                break
    return keywords


def _memory_extract_request_text(path: str, body: Json) -> str:
    from .gateway_protocol import _text_from_content
    parts: list[str] = []
    if path in {"/v1/chat/completions", "/v1/messages"}:
        for message in body.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = message.get("role") or ""
            text = _text_from_content(message.get("content"))
            if text:
                parts.append(f"{role}: {text}")
        system = body.get("system")
        if system:
            parts.append("system: " + _text_from_content(system))
    elif path == "/v1/responses":
        if body.get("instructions"):
            parts.append("system: " + _text_from_content(body.get("instructions")))
        parts.append("input: " + _text_from_content(body.get("input")))
    else:
        from .gateway_protocol import _last_user_text
        parts.append(_last_user_text(path, body))
    return "\n".join(part for part in parts if part.strip())


def _memory_summarize_turn(path: str, body: Json, response: Json | None, *, max_chars: int) -> tuple[str, str, list[str], int]:
    from .gateway_protocol import _text_from_content, _last_user_text
    from .gateway_builtin_tools import _response_text
    user_text = _last_user_text(path, body).strip()
    request_text = _memory_extract_request_text(path, body)
    response_text = _response_text(path, response or {}) if isinstance(response, dict) else ""
    keywords = _memory_extract_keywords("\n".join([request_text, response_text]))
    kind = "conversation_turn"
    importance = 1
    lowered = user_text.lower()
    if any(token in lowered for token in ("修改", "写", "实现", "fix", "edit", "write", "测试", "运行", "error", "报错")):
        kind = "implementation_context"
        importance = 3
    elif any(token in lowered for token in ("分析", "analyze", "项目", "代码", "class", "类")):
        kind = "analysis_context"
        importance = 2
    source_text = user_text or request_text
    if len(source_text) > max_chars:
        source_text = "[gateway context compacted]\n" + source_text
    if response_text:
        summary = f"用户请求：{_trim_text_for_context(source_text, max_chars // 2)}\n助手结论：{_trim_text_for_context(response_text, max_chars // 2)}"
    else:
        summary = f"用户请求：{_trim_text_for_context(source_text, max_chars)}"
    summary = _trim_text_for_context(summary, max_chars)
    return kind, summary, keywords, importance


def _sqlite_insert_memory(session_key: str, workspace_root: str, kind: str, summary: str, keywords: list[str], source_request_id: str | None, importance: int) -> None:
    from .gateway_logging import _sqlite_init, _sqlite_connect
    import datetime as _dt
    _sqlite_init()
    fields = _memory_index_fields(session_key, workspace_root)
    conn = _sqlite_connect()
    try:
        _sqlite_backfill_memory_index_fields(conn)
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO conversation_memories
                (
                    ts, session_key, workspace_root, tenant_key, workspace_key, memory_session_key,
                    kind, summary, keywords_json, source_request_id, importance, last_used_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                session_key,
                workspace_root,
                fields["tenant_key"],
                fields["workspace_key"],
                fields["memory_session_key"],
                kind,
                summary,
                json.dumps(keywords, ensure_ascii=False),
                source_request_id,
                importance,
                now,
            ),
        )
        conn.commit()
        if kind == "session_rollup":
            try:
                from .gateway_agent_planner import record_runtime_event
                record_runtime_event(
                    session_key=session_key,
                    tenant_key=fields["tenant_key"],
                    workspace_key=fields["workspace_key"],
                    event_type="memory_rollup",
                    workflow="memory",
                    step="session_rollup",
                    summary=summary[:500],
                    metadata={"importance": importance, "keywords": keywords[:20]},
                )
            except Exception:
                pass
    finally:
        conn.close()


def _memory_rollup_every() -> int:
    cfg = _memory_config()
    raw = cfg.get("memory_rollup_every_turns", os.environ.get("GATEWAY_MEMORY_ROLLUP_EVERY_TURNS", 8))
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 8


def _memory_rollup_max_chars() -> int:
    cfg = _memory_config()
    raw = cfg.get("memory_rollup_max_chars", os.environ.get("GATEWAY_MEMORY_ROLLUP_MAX_CHARS", 4000))
    try:
        return max(500, int(raw or 4000))
    except (TypeError, ValueError):
        return 4000


def _sqlite_latest_rollup(session_key: str, workspace_root: str) -> Json | None:
    from .gateway_logging import _sqlite_init, _sqlite_connect
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        _sqlite_backfill_memory_index_fields(conn)
        fields = _memory_index_fields(session_key, workspace_root)
        row = conn.execute(
            """
            SELECT id, ts, summary, keywords_json, importance, last_used_at
            FROM conversation_memories
            WHERE tenant_key = ? AND workspace_key = ? AND memory_session_key = ? AND kind = 'session_rollup'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (fields["tenant_key"], fields["workspace_key"], fields["memory_session_key"]),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "ts": row[1],
            "kind": "session_rollup",
            "summary": row[2],
            "keywords": json.loads(row[3]),
            "importance": row[4],
            "last_used_at": row[5],
        }
    finally:
        conn.close()


def _sqlite_recent_memories_since_rollup(session_key: str, workspace_root: str, *, limit: int = 200) -> list[Json]:
    from .gateway_logging import _sqlite_init, _sqlite_connect
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        _sqlite_backfill_memory_index_fields(conn)
        fields = _memory_index_fields(session_key, workspace_root)
        latest_rollup = _sqlite_latest_rollup(session_key, workspace_root)
        since_ts = str((latest_rollup or {}).get("ts") or "")
        if since_ts:
            rows = conn.execute(
                f"""
                SELECT id, ts, kind, summary, keywords_json, importance, last_used_at
                FROM conversation_memories
                WHERE tenant_key = ? AND workspace_key = ? AND memory_session_key = ?
                  AND kind != 'session_rollup' AND ts > ?
                ORDER BY ts ASC
                LIMIT ?
                """,
                (fields["tenant_key"], fields["workspace_key"], fields["memory_session_key"], since_ts, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT id, ts, kind, summary, keywords_json, importance, last_used_at
                FROM conversation_memories
                WHERE tenant_key = ? AND workspace_key = ? AND memory_session_key = ?
                  AND kind != 'session_rollup'
                ORDER BY ts ASC
                LIMIT ?
                """,
                (fields["tenant_key"], fields["workspace_key"], fields["memory_session_key"], limit),
            ).fetchall()
        return [
            {
                "id": row[0],
                "ts": row[1],
                "kind": row[2],
                "summary": row[3],
                "keywords": json.loads(row[4]),
                "importance": row[5],
                "last_used_at": row[6],
            }
            for row in rows
        ]
    finally:
        conn.close()


def _memory_build_rollup_summary(rows: list[Json], *, max_chars: int) -> str:
    if not rows:
        return ""
    messages = [
        {"role": "user", "content": f"{row.get('kind')}: {row.get('summary', '')}"}
        for row in rows[-20:]
    ]
    from .gateway_config import _env_bool
    if _env_bool("GATEWAY_MEMORY_ROLLUP_LLM_SUMMARY", False):
        summary = _summarize_via_llm(messages, max_summary_tokens=max(300, min(1200, max_chars // 4)))
        if summary:
            return _trim_text_for_context("[Periodic conversation summary]\n" + summary, max_chars)
    parts = ["[Periodic conversation summary]"]
    for row in rows[-20:]:
        parts.append(f"- {row.get('kind')}: {row.get('summary', '')}")
    return _trim_text_for_context("\n".join(parts), max_chars)


def _maybe_rollup_conversation_memory(session_key: str, workspace_root: str) -> None:
    every = _memory_rollup_every()
    if every <= 0:
        return
    rows = _sqlite_recent_memories_since_rollup(session_key, workspace_root, limit=max(every * 2, every))
    if len(rows) < every:
        return
    max_chars = _memory_rollup_max_chars()
    rollup = _memory_build_rollup_summary(rows, max_chars=max_chars)
    if not rollup:
        return
    keywords = _memory_extract_keywords(rollup)
    _sqlite_insert_memory(
        session_key,
        workspace_root,
        "session_rollup",
        rollup,
        keywords,
        None,
        5,
    )


def _remember_conversation_turn(path: str, body: Json, response: Json | None, *, source_request_id: str | None = None) -> None:
    if not _memory_enabled():
        return
    cfg = _memory_config()
    max_chars = cfg.get("memory_summary_max_chars", 900)
    kind, summary, keywords, importance = _memory_summarize_turn(path, body, response, max_chars=max_chars)
    if not summary:
        return
    session_key = _memory_session_key(body)
    workspace_root = _memory_workspace_key(body)
    _sqlite_insert_memory(session_key, workspace_root, kind, summary, keywords, source_request_id, importance)
    _maybe_rollup_conversation_memory(session_key, workspace_root)


def _prepend_latest_rollup(session_key: str, workspace_root: str, memories: list[Json], limit: int) -> list[Json]:
    rollup = _sqlite_latest_rollup(session_key, workspace_root)
    if not rollup:
        return memories
    rollup_id = rollup.get("id")
    filtered = [mem for mem in memories if mem.get("id") != rollup_id]
    return [rollup] + filtered[: max(0, limit - 1)]


def _sqlite_recall_memories(session_key: str, workspace_root: str, query_keywords: list[str], limit: int) -> list[Json]:
    from .gateway_logging import _sqlite_init, _sqlite_connect
    import datetime as _dt
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        _sqlite_backfill_memory_index_fields(conn)
        fields = _memory_index_fields(session_key, workspace_root)
        rows = conn.execute(
            """
            SELECT id, ts, kind, summary, keywords_json, importance, last_used_at
            FROM conversation_memories
            WHERE tenant_key = ? AND workspace_key = ? AND memory_session_key = ?
            ORDER BY importance DESC, ts DESC
            LIMIT ?
            """,
            (fields["tenant_key"], fields["workspace_key"], fields["memory_session_key"], limit * 2),
        ).fetchall()
        scored = []
        for row in rows:
            memory_keywords = json.loads(row[4])
            overlap = len(set(query_keywords) & set(memory_keywords))
            score = overlap + row[5] * 2
            scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        result = []
        for score, row in scored[:limit]:
            conn.execute(
                "UPDATE conversation_memories SET last_used_at = ? WHERE id = ?",
                (_dt.datetime.now(_dt.timezone.utc).isoformat(), row[0]),
            )
            result.append({
                "id": row[0],
                "ts": row[1],
                "kind": row[2],
                "summary": row[3],
                "keywords": json.loads(row[4]),
                "importance": row[5],
                "last_used_at": row[6],
            })
        conn.commit()
        return result
    finally:
        conn.close()


def _recall_conversation_memories(path: str, body: Json) -> list[Json]:
    if not _memory_enabled():
        return []
    cfg = _memory_config()
    limit = cfg.get("memory_recall_limit", 8)
    session_key = _memory_session_key(body)
    workspace_root = _memory_workspace_key(body)
    user_text = _memory_extract_request_text(path, body)
    # Use smart memory search with relevance scoring when query is available
    if user_text.strip():
        try:
            return _prepend_latest_rollup(session_key, workspace_root, _smart_memory_search(session_key, workspace_root, user_text, limit), limit)
        except Exception:
            pass
    # Fallback to keyword-based search
    keywords = _memory_extract_keywords(user_text)
    return _prepend_latest_rollup(session_key, workspace_root, _sqlite_recall_memories(session_key, workspace_root, keywords, limit), limit)


def _memory_block(memories: list[Json]) -> str:
    if not memories:
        return ""
    parts = ["[Gateway recalled memory]", "[Conversation Memories]"]
    for mem in memories:
        summary = " ".join(str(mem.get('summary', '')).split())
        parts.append(f"- {summary}")
    return "\n".join(parts) + "\n\n"


def _allocate_context_budget(task_type: str) -> dict[str, int]:
    budgets = {
        "coding": {"system": 2000, "tools": 4000, "history": 15000, "user": 3000},
        "analysis": {"system": 1500, "tools": 2000, "history": 20000, "user": 5000},
        "chat": {"system": 1000, "tools": 1000, "history": 20000, "user": 5000},
        "default": {"system": 1500, "tools": 3000, "history": 15000, "user": 5000},
    }
    return budgets.get(task_type, budgets["default"])


def _detect_task_type(user_text: str) -> str:
    text_lower = user_text.lower()
    if any(kw in text_lower for kw in ["code", "function", "class", "implement", "fix", "debug", "refactor"]):
        return "coding"
    if any(kw in text_lower for kw in ["analyze", "explain", "compare", "evaluate", "research"]):
        return "analysis"
    return "chat"


def _inject_recalled_memories(path: str, body: Json) -> Json:
    if not _memory_enabled():
        return body
    memories = _recall_conversation_memories(path, body)
    if not memories:
        return body
    cfg = _context_config()
    max_chars = cfg.get("memory_inject_max_chars", 4000)
    memory_text = _memory_block(memories)
    if len(memory_text) > max_chars:
        memory_text = memory_text[:max_chars] + "..."
    body = __import__("copy").deepcopy(body)
    messages = body.get("messages") or []
    if "/responses" in path:
        raw_input = body.get("input")
        if isinstance(raw_input, str):
            body["input"] = [
                {"role": "system", "content": memory_text},
                {"role": "user", "content": raw_input},
            ]
        elif isinstance(raw_input, list):
            input_items = list(raw_input)
            if input_items and isinstance(input_items[0], dict) and input_items[0].get("role") == "system":
                input_items[0]["content"] = memory_text + str(input_items[0].get("content") or "")
            else:
                input_items.insert(0, {"role": "system", "content": memory_text})
            body["input"] = input_items
        else:
            body["input"] = [{"role": "system", "content": memory_text}]
    elif "/messages" in path:
        if messages and isinstance(messages[0], dict):
            if messages[0].get("role") == "user":
                content = messages[0].get("content")
                if isinstance(content, list):
                    content.insert(0, {"type": "text", "text": memory_text})
                elif isinstance(content, str):
                    messages[0]["content"] = [{"type": "text", "text": memory_text + content}]
            else:
                # First message is not user; prepend a user message with memories
                messages.insert(0, {"role": "user", "content": [{"type": "text", "text": memory_text}]})
        elif not messages:
            messages.append({"role": "user", "content": [{"type": "text", "text": memory_text}]})
    else:
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages[0]["content"] = memory_text + str(messages[0].get("content") or "")
        else:
            messages.insert(0, {"role": "system", "content": memory_text})
    body["messages"] = messages
    return body


def _sqlite_tail_memories(
    limit: int = 50,
    workspace_root: str | None = None,
    *,
    tenant_key: str | None = None,
    tenant_contains: str | None = None,
    workspace_contains: str | None = None,
    session_contains: str | None = None,
    kind: str | None = None,
    has_rollup: bool | None = None,
) -> list[Json]:
    from .gateway_logging import _sqlite_init, _sqlite_connect
    try:
        limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    tenant_exact = str(tenant_key or "").strip()
    tenant_filter = str(tenant_contains or "").strip().lower()
    workspace_filter = str(workspace_contains or "").strip().lower()
    session_filter = str(session_contains or "").strip().lower()
    kind_filter = str(kind or "").strip()
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        _sqlite_backfill_memory_index_fields(conn)
        where: list[str] = []
        params: list[Any] = []
        if workspace_root:
            where.append("workspace_key = ?")
            params.append(workspace_root)
        if tenant_exact:
            where.append("tenant_key = ?")
            params.append(tenant_exact)
        if tenant_filter:
            where.append("LOWER(tenant_key) LIKE ?")
            params.append(f"%{tenant_filter}%")
        if workspace_filter:
            where.append("LOWER(workspace_key) LIKE ?")
            params.append(f"%{workspace_filter}%")
        if session_filter:
            where.append("(LOWER(session_key) LIKE ? OR LOWER(memory_session_key) LIKE ?)")
            params.extend([f"%{session_filter}%", f"%{session_filter}%"])
        if kind_filter:
            where.append("kind = ?")
            params.append(kind_filter)
        if has_rollup is True:
            where.append("kind = 'session_rollup'")
        elif has_rollup is False:
            where.append("kind != 'session_rollup'")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"""
            SELECT id, ts, session_key, workspace_root, tenant_key, workspace_key, memory_session_key,
                   kind, summary, keywords_json, source_request_id, importance, last_used_at
            FROM conversation_memories
            {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [
            {
                "id": row[0],
                "ts": row[1],
                "session_key": row[2],
                "workspace_root": row[3],
                "tenant_key": row[4],
                "workspace_key": row[5],
                "memory_session_key": row[6],
                "kind": row[7],
                "summary": row[8],
                "keywords": json.loads(row[9]),
                "source_request_id": row[10],
                "importance": row[11],
                "last_used_at": row[12],
            }
            for row in rows
        ]
    finally:
        conn.close()


# Context compaction
def _upstream_supports_native_tools() -> bool:
    from .gateway_config import _upstream_config
    cfg = _upstream_config()
    capabilities = cfg.get("capabilities", {})
    return capabilities.get("supports_tools", False)


def _summarize_via_llm(messages: list[Json], *, max_summary_tokens: int = 800) -> str | None:
    content_key = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(content_key.encode()).hexdigest()[:16]
    cached = _summary_cache_get(content_hash)
    if cached is not None:
        return cached
    from .gateway_config import _upstream_config, _upstream_protocol
    from .gateway_protocol import _text_from_content, _to_openai_chat_payload
    from .gateway_logging import _record_request_stat, _write_request_log
    try:
        text_parts = []
        for msg in messages[-10:]:
            if isinstance(msg, dict):
                content = msg.get("content")
                text = _text_from_content(content)
                if text:
                    role = msg.get("role", "unknown")
                    text_parts.append(f"{role}: {text[:500]}")
        if not text_parts:
            return None
        summary_text = "\n".join(text_parts)
        cfg = _upstream_config()
        protocol = _upstream_protocol()
        if protocol == "anthropic_messages":
            payload = {
                "model": cfg.get("model", ""),
                "max_tokens": max_summary_tokens,
                "messages": [{"role": "user", "content": f"Summarize this conversation concisely:\n\n{summary_text}"}],
            }
        else:
            payload = {
                "model": cfg.get("model", ""),
                "max_tokens": max_summary_tokens,
                "messages": [
                    {"role": "system", "content": "Summarize conversations concisely."},
                    {"role": "user", "content": f"Summarize this conversation:\n\n{summary_text}"},
                ],
            }
        from .gateway_proxy import NativeProxyClient
        client = NativeProxyClient()
        # Context summarization is optional best-effort work.  Do not use the
        # normal proxy retry loop here: local/dev configs can temporarily point
        # at an unavailable upstream (or even the Gateway itself), and a summary
        # fallback must return promptly instead of blocking tests/requests for
        # the proxy's long transient-error retry window.
        try:
            client.timeout = min(float(client.timeout or 60.0), 3.0)
        except (TypeError, ValueError):
            client.timeout = 3.0
        def post_once(request_path: str, request_body: Json) -> Json:
            data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
            return client._do_request_once("POST", client._url(request_path), client._headers(), data)
        if protocol == "anthropic_messages":
            response = post_once("/v1/messages", payload)
            content = response.get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    summary = str(item.get("text") or "")
                    _summary_cache_put(content_hash, summary)
                    return summary
        else:
            response = post_once("/v1/chat/completions", payload)
            choices = response.get("choices") or []
            if choices:
                summary = choices[0].get("message", {}).get("content")
                if summary:
                    _summary_cache_put(content_hash, summary)
                return summary
    except Exception:
        pass
    return None


def _compact_messages_with_summary(messages: list[Json], *, keep_recent: int, text_limit: int) -> list[Json]:
    if len(messages) <= keep_recent:
        return messages
    old_messages = messages[:-keep_recent]
    recent_messages = []
    for msg in messages[-keep_recent:]:
        if isinstance(msg, dict):
            updated = dict(msg)
            if "content" in updated:
                updated["content"] = _trim_content_for_context(updated.get("content"), text_limit)
            if "input" in updated:
                updated["input"] = _trim_content_for_context(updated.get("input"), text_limit)
            recent_messages.append(updated)
        else:
            recent_messages.append(msg)
    summary = _summarize_via_llm(old_messages)
    if summary:
        compacted = [{"role": "system", "content": f"[Previous conversation summary]\n{summary}"}]
    else:
        # Best-effort extractive fallback: keep a bounded role-labelled digest
        # instead of dropping old turns entirely.  This is the non-LLM path that
        # makes "infinite context" degrade into a rolling summary rather than a
        # hard amnesia boundary when the summary upstream is unavailable.
        from .gateway_protocol import _text_from_content
        parts: list[str] = []
        for msg in old_messages[-20:]:
            if not isinstance(msg, dict):
                continue
            text = _text_from_content(msg.get("content"))
            if not text:
                continue
            role = str(msg.get("role") or "unknown")
            parts.append(f"{role}: {_trim_text_for_context(text, max(200, text_limit // 4))}")
        digest = "\n".join(parts)
        compacted = [{
            "role": "system",
            "content": "[Previous conversation compacted - summary unavailable]\n" + _trim_text_for_context(digest, text_limit),
        }]
    compacted.extend(recent_messages)
    return compacted


def _trim_text_for_context(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...(truncated)"


def _trim_content_for_context(content: Any, limit: int) -> Any:
    if isinstance(content, str):
        return _trim_text_for_context(content, limit)
    if isinstance(content, list):
        result = []
        for item in content:
            if isinstance(item, dict):
                updated = dict(item)
                if updated.get("type") == "text":
                    text = updated.get("text", "")
                    if len(text) > limit:
                        updated["text"] = _trim_text_for_context(text, limit)
                if "content" in updated:
                    updated["content"] = _trim_content_for_context(updated.get("content"), limit)
                result.append(updated)
            elif isinstance(item, str):
                result.append(_trim_text_for_context(item, limit))
            else:
                result.append(item)
        return result
    if isinstance(content, dict):
        updated = dict(content)
        if updated.get("type") == "text":
            text = updated.get("text", "")
            if len(text) > limit:
                updated["text"] = _trim_text_for_context(text, limit)
        if "content" in updated:
            updated["content"] = _trim_content_for_context(updated.get("content"), limit)
        return updated
    return content


def _content_text_length(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
                total += len(str(item.get("text") or ""))
            elif isinstance(item, str):
                total += len(item)
        return total
    if isinstance(content, dict) and content.get("type") == "text":
        return len(str(content.get("text") or ""))
    return len(str(content)) if content is not None else 0


def _compact_messages(messages: Any, *, keep_recent: int, text_limit: int) -> list[Json]:
    if not isinstance(messages, list):
        return messages
    result = []
    huge_message_limit = max(text_limit * 2, text_limit)
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            result.append(msg)
            continue
        content = msg.get("content")
        should_trim = i < len(messages) - keep_recent or _content_text_length(content) > huge_message_limit
        if should_trim:
            trimmed = _trim_content_for_context(content, text_limit)
            result.append({**msg, "content": trimmed})
        else:
            result.append(msg)
    return result


def _compact_request_for_upstream(path: str, body: Json, cfg: Json, *, reason: str = "over_limit") -> Json:
    """Remove bulky downstream harness metadata while preserving user intent."""
    from .gateway_protocol import _without_tools
    had_tools = bool(body.get("tools")) or body.get("tool_choice") not in (None, "", "none")
    updated = _without_tools(body)
    for key in ("metadata", "thinking", "output_config"):
        updated.pop(key, None)
    keep_recent = int(cfg.get("keep_recent_messages") or 12)
    summary_limit = int(cfg.get("summary_max_chars") or 6000)
    if path in {"/v1/chat/completions", "/v1/messages"}:
        messages = updated.get("messages")
        if isinstance(messages, list) and len(messages) > keep_recent:
            compacted_messages = _compact_messages_with_summary(
                messages,
                keep_recent=keep_recent,
                text_limit=summary_limit,
            )
        else:
            compacted_messages = _compact_messages(messages, keep_recent=keep_recent, text_limit=summary_limit)
        if not isinstance(compacted_messages, list):
            compacted_messages = []
        summary_text = ""
        if (
            compacted_messages
            and isinstance(compacted_messages[0], dict)
            and compacted_messages[0].get("role") == "system"
        ):
            summary_text = str(compacted_messages[0].get("content") or "")
            compacted_messages = compacted_messages[1:]
        if path == "/v1/messages":
            from .gateway_protocol import _text_from_content
            existing_system_raw = updated.get("system")
            if isinstance(existing_system_raw, str):
                existing_system = existing_system_raw
            else:
                existing_system = _text_from_content(existing_system_raw)
            existing_system = _trim_text_for_context(str(existing_system or ""), summary_limit)
            if summary_text:
                existing_system = (existing_system + "\n\n" + summary_text).strip() if existing_system else summary_text
            updated["messages"] = compacted_messages
            updated["system"] = (_gateway_system_prompt(reason) + "\n\n" + existing_system).strip()
        else:
            system_text = _gateway_system_prompt(reason)
            if summary_text:
                system_text += "\n\n" + summary_text
            compacted_messages.insert(0, {"role": "system", "content": system_text})
            updated["messages"] = compacted_messages
    else:
        existing = updated.get("input")
        if isinstance(existing, str):
            updated["input"] = _trim_text_for_context(existing, summary_limit)
        elif isinstance(existing, list):
            if len(existing) > keep_recent:
                updated["input"] = _compact_messages_with_summary(
                    existing,
                    keep_recent=keep_recent,
                    text_limit=summary_limit,
                )
            else:
                updated["input"] = _trim_content_for_context(existing, summary_limit)
        updated["instructions"] = _gateway_system_prompt(reason)
    updated.setdefault("gateway_context", {})
    updated["gateway_context"].update({
        "compacted": True,
        "reason": reason,
        "original_estimated_tokens": _body_token_estimate(body),
        "had_tools": had_tools,
    })
    return updated


def _maybe_compact_request_for_upstream(path: str, body: Json, cfg: Json, *, reason: str = "over_limit") -> Json:
    if not cfg.get("enabled"):
        return body
    max_tokens = int(cfg.get("max_input_tokens") or 24000)
    if _body_token_estimate(body) <= max_tokens:
        return body
    return _compact_request_for_upstream(path, body, cfg, reason=reason)


# Fanout system
def _chunk_text_by_tokens(text: str, chunk_tokens: int, max_chunks: int) -> list[str]:
    chars_per_chunk = chunk_tokens * 4
    if len(text) <= chars_per_chunk:
        return [text]
    chunks = []
    start = 0
    while start < len(text) and (max_chunks == 0 or len(chunks) < max_chunks):
        end = min(start + chars_per_chunk, len(text))
        chunks.append(text[start:end])
        start = end
    return chunks


def _fanout_source_text(path: str, body: Json) -> str:
    from .gateway_protocol import _text_from_content
    messages = body.get("messages") or []
    parts = []
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content")
            text = _text_from_content(content)
            if text:
                parts.append(text)
    return "\n".join(parts)


def _make_partial_prompt(original_prompt: str, chunk: str, index: int, total: int) -> str:
    return f"片段 {index + 1}/{total}\n\n{chunk}"


def _trim_partials_for_synthesis(partials: list[str], *, total_budget: int = 30000) -> list[str]:
    if not partials:
        return partials
    per_partial = total_budget // len(partials)
    return [p[:per_partial] for p in partials]


def _make_synthesis_prompt(original_prompt: str, partials: list[str]) -> str:
    original_compact = _trim_text_for_context(original_prompt, 2000)
    trimmed = _trim_partials_for_synthesis(partials)
    parts = [f"子分析 {i + 1}:\n{p}" for i, p in enumerate(trimmed)]
    return (
        "原始用户问题（压缩）:\n"
        f"[gateway context compacted]\n{original_compact}\n\n"
        "请综合以下分片结果，给出完整、准确、可执行的最终回答。\n\n"
        f"{chr(10).join(parts)}"
    )


def _make_quality_review_prompt(original_prompt: str, draft_text: str) -> str:
    original_compact = _trim_text_for_context(original_prompt, 1000)
    return (
        "质量审查器：请审查以下草稿的质量和完整性。\n\n"
        f"原始用户问题（压缩）:\n[gateway context compacted]\n{original_compact}\n\n"
        f"草稿:\n{draft_text[:5000]}"
    )


def _should_fanout_context(path: str, body: Json, cfg: Json, *, force: bool = False) -> bool:
    context_cfg = cfg.get("context", {})
    if not context_cfg.get("fanout_enabled", True):
        return False
    if force:
        return True
    max_input = context_cfg.get("max_input_tokens", 24000)
    current_tokens = _body_token_estimate(body)
    return current_tokens > max_input * 2


def _run_context_fanout(path: str, body: Json, upstream: Any, cfg: Json, *, force: bool = False) -> Json | None:
    if not _should_fanout_context(path, body, cfg, force=force):
        return None
    context_cfg = cfg.get("context", {})
    chunk_tokens = context_cfg.get("fanout_chunk_tokens", 12000)
    max_chunks = context_cfg.get("fanout_max_chunks", 0)
    if force and max_chunks == 0:
        max_chunks = int(context_cfg.get("forced_fanout_max_chunks") or 4)
    max_workers = context_cfg.get("fanout_max_workers", 4)
    quality_review = context_cfg.get("quality_review_enabled", True)
    strategy = "fanout_forced_synthesis" if force else "fanout_synthesis"
    source_text = _fanout_source_text(path, body)
    chunks = _chunk_text_by_tokens(source_text, chunk_tokens, max_chunks)
    if len(chunks) <= 1:
        return None
    import concurrent.futures
    # Use forward() if available (for FakeClient compatibility), otherwise post()
    upstream_fn = getattr(upstream, 'forward', None) or getattr(upstream, 'post', None)
    if upstream_fn is None:
        return None
    partials = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, chunk in enumerate(chunks):
            partial_body = __import__("copy").deepcopy(body)
            messages = partial_body.get("messages") or []
            if messages:
                last_msg = messages[-1]
                if isinstance(last_msg, dict) and last_msg.get("role") == "user":
                    last_msg["content"] = _make_partial_prompt(source_text, chunk, i, len(chunks))
            futures.append(executor.submit(upstream_fn, path, partial_body))
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                from .gateway_protocol import _text_from_content
                # Support both OpenAI and Anthropic response formats
                text = None
                choices = result.get("choices")
                if choices:
                    text = _text_from_content(choices[0].get("message", {}).get("content"))
                if not text:
                    content = result.get("content")
                    if isinstance(content, list):
                        text = _text_from_content(content)
                if not text:
                    text = result.get("output_text") or result.get("text")
                if text:
                    partials.append(text)
            except Exception:
                continue
    if not partials:
        return None
    synthesis_body = __import__("copy").deepcopy(body)
    messages = synthesis_body.get("messages") or []
    if messages:
        last_msg = messages[-1]
        if isinstance(last_msg, dict) and last_msg.get("role") == "user":
            last_msg["content"] = _make_synthesis_prompt(source_text, partials)
    synthesis_result = upstream_fn(path, synthesis_body)
    # Quality review step
    if quality_review and synthesis_result:
        from .gateway_protocol import _text_from_content
        synthesis_text = None
        choices = synthesis_result.get("choices")
        if choices:
            synthesis_text = _text_from_content(choices[0].get("message", {}).get("content"))
        if not synthesis_text:
            content = synthesis_result.get("content")
            if isinstance(content, list):
                synthesis_text = _text_from_content(content)
        if not synthesis_text:
            synthesis_text = synthesis_result.get("output_text") or synthesis_result.get("text")
        if synthesis_text:
            review_body = __import__("copy").deepcopy(body)
            review_messages = review_body.get("messages") or []
            if review_messages:
                last_msg = review_messages[-1]
                if isinstance(last_msg, dict) and last_msg.get("role") == "user":
                    last_msg["content"] = _make_quality_review_prompt(source_text, synthesis_text)
            review_result = upstream_fn(path, review_body)
            if review_result:
                review_text = None
                choices = review_result.get("choices")
                if choices:
                    review_text = _text_from_content(choices[0].get("message", {}).get("content"))
                if not review_text:
                    content = review_result.get("content")
                    if isinstance(content, list):
                        review_text = _text_from_content(content)
                if not review_text:
                    review_text = review_result.get("output_text") or review_result.get("text")
                if review_text:
                    # Use the reviewed text as the final result
                    if choices:
                        synthesis_result["choices"][0]["message"]["content"] = review_text
                    synthesis_result["gateway_context"] = {
                        "strategy": strategy,
                        "chunks": len(chunks),
                        "quality_reviewed": True,
                    }
                    return synthesis_result
    synthesis_result["gateway_context"] = {
        "strategy": strategy,
        "chunks": len(chunks),
        "quality_reviewed": False,
    }
    return synthesis_result


# =============================================================================
# Enhanced context management for infinite context
# =============================================================================

def _calculate_message_importance(message: Json) -> float:
    """Calculate importance score for a message (0.0 to 1.0)."""
    if not isinstance(message, dict):
        return 0.5

    role = message.get("role", "")
    content = message.get("content", "")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text += str(item.get("text") or "")

    text_lower = text.lower()
    score = 0.5

    # System messages are important
    if role == "system":
        score += 0.3

    # Messages with tool calls/results are important
    if message.get("tool_calls") or role == "tool":
        score += 0.2

    # Messages with error keywords are important
    if any(kw in text_lower for kw in ["error", "bug", "fix", "important", "critical", "warning"]):
        score += 0.15

    # Messages with code are important
    if "```" in text or "def " in text or "class " in text:
        score += 0.1

    # Recent messages are more important (handled elsewhere)

    return min(1.0, score)


def _progressive_compact_messages(messages: list[Json], *, target_tokens: int) -> list[Json]:
    """Progressively compact messages based on importance."""
    if not messages:
        return messages

    # Calculate current tokens
    current_tokens = sum(_approx_token_count(msg) for msg in messages)
    if current_tokens <= target_tokens:
        return messages

    # Score all messages
    scored = [(i, _calculate_message_importance(msg), msg) for i, msg in enumerate(messages)]

    # Sort by importance (ascending) - least important first
    scored.sort(key=lambda x: x[1])

    # Remove least important messages until we're under budget
    removed_indices = set()
    for i, score, msg in scored:
        if current_tokens <= target_tokens:
            break
        msg_tokens = _approx_token_count(msg)
        if msg_tokens > 100:  # Only remove substantial messages
            removed_indices.add(i)
            current_tokens -= msg_tokens

    # Rebuild messages without removed ones
    result = []
    for i, msg in enumerate(messages):
        if i not in removed_indices:
            result.append(msg)

    return result


def _smart_memory_search(session_key: str, workspace_root: str, query: str, limit: int) -> list[Json]:
    """Smart memory search with relevance scoring."""
    from .gateway_logging import _sqlite_init, _sqlite_connect
    import datetime as _dt

    _sqlite_init()
    conn = _sqlite_connect()
    try:
        # Get all memories for this explicit remote tenant/workspace/session scope.
        _sqlite_backfill_memory_index_fields(conn)
        fields = _memory_index_fields(session_key, workspace_root)
        rows = conn.execute(
            """
            SELECT id, ts, kind, summary, keywords_json, importance, last_used_at
            FROM conversation_memories
            WHERE tenant_key = ? AND workspace_key = ? AND memory_session_key = ?
            ORDER BY ts DESC
            LIMIT 100
            """,
            (fields["tenant_key"], fields["workspace_key"], fields["memory_session_key"]),
        ).fetchall()

        if not rows:
            return []

        # Calculate relevance scores
        query_lower = query.lower()
        query_words = set(re.findall(r'\b\w{3,}\b', query_lower))

        scored = []
        for row in rows:
            memory_summary = row[3].lower()
            memory_keywords = json.loads(row[4])

            # Base score from importance
            score = row[5] * 2

            # Keyword overlap
            keyword_overlap = len(query_words & set(memory_keywords))
            score += keyword_overlap * 3

            # Text similarity (simple word overlap)
            memory_words = set(re.findall(r'\b\w{3,}\b', memory_summary))
            text_overlap = len(query_words & memory_words)
            score += text_overlap * 2

            # Recency bonus (more recent = higher score)
            try:
                memory_time = __import__("datetime").datetime.fromisoformat(row[1])
                age_hours = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) - memory_time).total_seconds() / 3600
                recency_bonus = max(0, 1.0 - (age_hours / 168))  # Decay over 1 week
                score += recency_bonus
            except Exception:
                pass

            scored.append((score, row))

        # Sort by score and return top results
        scored.sort(key=lambda x: x[0], reverse=True)

        # Update last_used_at for recalled memories
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        result = []
        for score, row in scored[:limit]:
            conn.execute(
                "UPDATE conversation_memories SET last_used_at = ? WHERE id = ?",
                (now, row[0]),
            )
            result.append({
                "id": row[0],
                "ts": row[1],
                "kind": row[2],
                "summary": row[3],
                "keywords": json.loads(row[4]),
                "importance": row[5],
                "last_used_at": row[6],
                "relevance_score": score,
            })

        conn.commit()
        return result
    finally:
        conn.close()


def _route_to_long_context_model(path: str, body: Json) -> bool:
    """Determine if request should be routed to a long-context model."""
    cfg = _context_config()
    if not cfg.get("route_to_long_context", True):
        return False

    long_context_upstream = cfg.get("long_context_upstream", {})
    if not long_context_upstream.get("base_url"):
        return False

    # Check if current context exceeds threshold
    max_input = cfg.get("max_input_tokens", 24000)
    current_tokens = _body_token_estimate(body)

    return current_tokens > max_input * 1.5


def _get_long_context_upstream() -> "NativeProxyClient" | None:
    """Get a proxy client for long-context model."""
    from .gateway_proxy import NativeProxyClient

    cfg = _context_config()
    long_context_upstream = cfg.get("long_context_upstream", {})

    if not long_context_upstream.get("base_url"):
        return None

    return NativeProxyClient(
        base_url=long_context_upstream["base_url"],
        api_key=long_context_upstream.get("api_key"),
        model=long_context_upstream.get("model"),
    )


def _adaptive_context_management(path: str, body: Json) -> Json:
    """Adaptive context management that chooses the best strategy."""
    from .gateway_config import load_config

    cfg = load_config()
    context_cfg = cfg.get("context", {})

    if not context_cfg.get("enabled", True):
        return body

    # Estimate current tokens
    current_tokens = _body_token_estimate(body)
    max_input = context_cfg.get("max_input_tokens", 24000)

    # Strategy 1: If way over limit, try long-context model
    if current_tokens > max_input * 3:
        if _route_to_long_context_model(path, body):
            return body  # Will be handled by long-context routing

    # Strategy 2: If moderately over limit, use progressive compaction
    if current_tokens > max_input * 1.5:
        target_tokens = int(max_input * 0.8)
        messages = body.get("messages") or []
        compacted = _progressive_compact_messages(messages, target_tokens=target_tokens)
        body = __import__("copy").deepcopy(body)
        body["messages"] = compacted
        return body

    # Strategy 3: If slightly over limit, use standard compaction
    if current_tokens > max_input:
        return _maybe_compact_request_for_upstream(path, body, cfg)

    return body
