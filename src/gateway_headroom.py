"""Headroom-style context compression for weak upstream limits.

Weak upstream relays (e.g. relay-only Claude/Anthropic proxies) often cap
request size well below the advertised context window.  When the body
exceeds that practical cap the upstream returns a generic
``Sorry, the text you sent is too long`` refusal and the user's intent is
lost.  This module implements a small set of transforms modeled on the
public headroom strategies (SmartCrusher row dropping, code-aware
summarization, log-line keying, message history trimming) so the gateway
can squeeze an oversized harness back under the cap without rewriting the
system prompt wholesale.

Design goals:

* **Non-destructive by default** — compression is layered; we only drop or
  shrink the most lossy content (large tool result bodies, log spam,
  middle-of-history tool turns) and keep the harness system prompt and
  tool definitions intact so the downstream model still sees a coherent
  conversation shape.
* **Budget-driven** — a single token target is enforced via progressively
  heavier transforms; the function returns the smallest body that fits
  the target, falling back to a hard replace only when nothing else
  works.
* **Pluggable** — the entry point ``headroom_compress`` accepts an
  already-prepared request body and a target budget and returns a new
  body.  Callers (tool runtime, streaming) can wire it in without
  changing the rest of the request pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

Json = dict[str, Any]

# ---------------------------------------------------------------------------
# Token estimation (mirrors gateway_context._approx_token_count but kept
# self-contained so this module can be unit tested without bootstrap).
# ---------------------------------------------------------------------------


def _approx_tokens(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        cjk = sum(1 for ch in value if "一" <= ch <= "鿿")
        other = max(len(value) - cjk, 0)
        return cjk + max(1, other // 4)
    if isinstance(value, (int, float, bool)):
        return 1
    if isinstance(value, dict):
        return _approx_tokens(value.get("content") or value.get("text") or json.dumps(value, ensure_ascii=False))
    if isinstance(value, list):
        return sum(_approx_tokens(item) for item in value)
    return 0


def _body_tokens(body: Json) -> int:
    return _approx_tokens({k: v for k, v in body.items() if k not in {"tools", "tool_choice"}})


# ---------------------------------------------------------------------------
# Content-type detection.
# ---------------------------------------------------------------------------


_JSON_ARRAY_RE = re.compile(r"\s*\[\s*\{.*\}\s*\]", re.DOTALL)
_LINE_BREAK = "\n"


def _is_json_array(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith("["):
        return False
    if not stripped.rstrip().endswith("]"):
        return False
    try:
        parsed = json.loads(stripped)
    except Exception:
        return False
    return isinstance(parsed, list) and parsed and all(isinstance(item, (dict, list)) for item in parsed[:8])


def _looks_like_code(text: str) -> bool:
    code_signals = ("def ", "class ", "import ", "function ", "const ", "let ", "var ", "=>", "{", "};", ");")
    if not text:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 4:
        return False
    hits = sum(1 for ln in lines if any(sig in ln for sig in code_signals))
    return hits >= max(2, len(lines) // 8)


def _looks_like_log(text: str) -> bool:
    if not text:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 6:
        return False
    log_tokens = (
        "ERROR",
        "WARN",
        "INFO",
        "DEBUG",
        "TRACE",
        "Exception",
        "Traceback",
        "stack trace",
        "at ",
        "FAIL",
    )
    hits = sum(1 for ln in lines if any(tok in ln for tok in log_tokens))
    return hits >= max(2, len(lines) // 12)


# ---------------------------------------------------------------------------
# SmartCrusher-style JSON array compression.
# ---------------------------------------------------------------------------


def _smart_crush_json_array(text: str, *, max_items: int) -> str:
    """Drop redundant middle rows from a JSON array of records.

    Mirrors headroom's SmartCrusher lossy path: keep the first and last
    ``max_items // 2`` rows, drop the middle, and emit a sentinel that
    the model can act on if it really needs the dropped rows.  The output
    is valid JSON so downstream code that iterates the array (e.g. log
    parsers) still works.
    """
    try:
        parsed = json.loads(text)
    except Exception:
        return text
    if not isinstance(parsed, list) or len(parsed) <= max_items:
        return text
    head = parsed[: max(1, max_items // 2)]
    tail = parsed[-max(1, max_items // 2):]
    dropped = len(parsed) - len(head) - len(tail)
    sentinel = {"_headroom_dropped": f"{dropped} rows collapsed (kept head {len(head)}, tail {len(tail)})"}
    crushed = head + [sentinel] + tail
    return json.dumps(crushed, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Repetitive-line deduplication.
# ---------------------------------------------------------------------------


def _deduplicate_repetitive_lines(text: str, *, keep_first: int = 5, keep_last: int = 5) -> str:
    """Collapse long runs of repeated lines into a single representative.

    Claude Code / Codex tool results often contain the same line echoed
    dozens of times (e.g. ``print("hello 0")`` repeated).  The unique-line
    set is small but the raw payload is large.  Keep the first few and
    the last few instances and emit a count sentinel for the rest.
    """
    lines = text.splitlines()
    if len(lines) <= keep_first + keep_last + 4:
        return text
    head = lines[:keep_first]
    tail = lines[-keep_last:]
    seen: set[str] = set()
    unique_count = 0
    for line in lines:
        if line.strip() and line.strip() not in seen:
            seen.add(line.strip())
            unique_count += 1
    middle_collapsed = len(lines) - len(head) - len(tail)
    sentinel = f"... [headroom deduplicated {middle_collapsed} of {len(lines)} lines; ~{unique_count} unique] ..."
    return _LINE_BREAK.join(head + [sentinel] + tail)


# ---------------------------------------------------------------------------
# Code-aware summarization.
# ---------------------------------------------------------------------------


def _summarize_code(text: str, *, head_lines: int, tail_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines + 4:
        return text
    head = lines[:head_lines]
    tail = lines[-tail_lines:]
    dropped = len(lines) - len(head) - len(tail)
    sentinel = f"... [headroom dropped {dropped} lines of code, see symbol index above] ..."
    return _LINE_BREAK.join(head + [sentinel] + tail)


# ---------------------------------------------------------------------------
# Log keying.
# ---------------------------------------------------------------------------


_LOG_KEEP_RE = re.compile(
    r"(?P<keep>(?:ERROR|WARN(?:ING)?|CRITICAL|FAIL(?:ED|URE)?|Exception|Traceback|panic|FATAL|^\s*at\s+|=>\s|\[error\]|\[warn\])[^\n]*)",
    re.MULTILINE,
)


def _key_log_lines(text: str, *, keep_lines: int) -> str:
    matches = _LOG_KEEP_RE.findall(text)
    if not matches:
        # No obvious log signal; fall back to a tail trim.
        lines = text.splitlines()
        if len(lines) <= keep_lines:
            return text
        return _LINE_BREAK.join(lines[-keep_lines:])
    seen: set[str] = set()
    kept: list[str] = []
    for line in matches:
        sig = line.strip()
        if sig in seen:
            continue
        seen.add(sig)
        kept.append(sig)
        if len(kept) >= keep_lines:
            break
    dropped = max(0, len(text.splitlines()) - len(kept))
    header = f"[headroom kept {len(kept)} unique log lines, dropped {dropped}]"
    return header + "\n" + "\n".join(kept)


# ---------------------------------------------------------------------------
# Message-aware transforms.
# ---------------------------------------------------------------------------


def _stringify_block(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return str(block.get("text") or block.get("content") or json.dumps(block, ensure_ascii=False))
    if isinstance(block, list):
        return "\n".join(_stringify_block(b) for b in block)
    return str(block)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_stringify_block(b) for b in content)
    return _stringify_block(content)


def _compress_tool_result_block(block: dict[str, Any], *, json_max_items: int, code_head: int, code_tail: int, log_lines: int) -> dict[str, Any]:
    """Compress a single tool_result block's payload while preserving tool_use_id.

    Empty/missing content is returned unchanged.  Otherwise we detect the
    content shape and apply the matching transform, returning a new block
    with the shrunken content and a ``gateway_compaction`` hint so the
    model knows this is intentional.
    """
    if not isinstance(block, dict):
        return block
    if block.get("type") != "tool_result":
        return block
    content = block.get("content")
    text = _content_text(content) if content is not None else ""
    if not text:
        return block
    new_text: str | None = None
    transform = ""
    if _is_json_array(text):
        crushed = _smart_crush_json_array(text, max_items=json_max_items)
        if crushed != text:
            new_text = crushed
            transform = "smart_crusher"
    if new_text is None and _looks_like_code(text):
        summarized = _summarize_code(text, head_lines=code_head, tail_lines=code_tail)
        if summarized != text:
            new_text = summarized
            transform = "code_summary"
    if new_text is None and _looks_like_log(text):
        keyed = _key_log_lines(text, keep_lines=log_lines)
        if keyed != text:
            new_text = keyed
            transform = "log_key"
    if new_text is None:
        # Last resort: deduplicate repetitive lines.  Many tool outputs (cat
        # on a templated file, repeated ``print`` lines, etc.) have very few
        # unique lines; collapsing them keeps the structure visible.
        deduped = _deduplicate_repetitive_lines(text, keep_first=5, keep_last=5)
        if deduped != text:
            new_text = deduped
            transform = "dedupe"
    if new_text is None:
        return block
    updated = dict(block)
    updated["content"] = new_text
    updated.setdefault("gateway_context", {})
    if isinstance(updated["gateway_context"], dict):
        updated["gateway_context"]["tool_result_compression"] = transform
    return updated


def _compress_message_content(path: str, content: Any, *, json_max_items: int, code_head: int, code_tail: int, log_lines: int) -> tuple[Any, int]:
    """Return (new_content, applied_count) for a user/assistant content field."""
    if not isinstance(content, list):
        return content, 0
    applied = 0
    new_blocks: list[Any] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            new_block = _compress_tool_result_block(
                block, json_max_items=json_max_items, code_head=code_head, code_tail=code_tail, log_lines=log_lines
            )
            if new_block is not block:
                applied += 1
            new_blocks.append(new_block)
        else:
            new_blocks.append(block)
    return new_blocks, applied


# ---------------------------------------------------------------------------
# History trimming.
# ---------------------------------------------------------------------------


def _trim_messages(messages: list[Any], *, keep_last: int) -> list[Any]:
    if keep_last <= 0 or len(messages) <= keep_last:
        return messages
    return messages[-keep_last:]


# ---------------------------------------------------------------------------
# Top-level entry point.
# ---------------------------------------------------------------------------


def headroom_compress(
    body: Json,
    *,
    target_tokens: int,
    json_max_items: int = 24,
    code_head_lines: int = 60,
    code_tail_lines: int = 40,
    log_keep_lines: int = 40,
    keep_recent_messages: int = 12,
) -> Json:
    """Compress ``body`` in place, returning a new body that fits ``target_tokens``.

    The strategy is layered, lightest to heaviest:

    1. Compress every tool_result block (JSON array / code / log).
    2. If still over budget, trim the message history to the most recent
       ``keep_recent_messages`` turns (preserving system prompt and
       tools untouched).
    3. If still over budget, replace the system prompt with a compact
       marker so the request at least reaches the upstream.
    """
    from copy import deepcopy

    if target_tokens <= 0:
        return body
    if _body_tokens(body) <= target_tokens:
        return body
    updated = deepcopy(body)

    # Stage 1: per-tool_result compression.
    if "/messages" in body.get("_path_hint", "") or True:
        messages = updated.get("messages")
        if isinstance(messages, list):
            new_messages: list[Any] = []
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") in {"user", "assistant"}:
                    new_content, _applied = _compress_message_content(
                        "/v1/messages",
                        msg.get("content"),
                        json_max_items=json_max_items,
                        code_head=code_head_lines,
                        code_tail=code_tail_lines,
                        log_lines=log_keep_lines,
                    )
                    new_msg = dict(msg)
                    new_msg["content"] = new_content
                    new_messages.append(new_msg)
                else:
                    new_messages.append(msg)
            updated["messages"] = new_messages

    if _body_tokens(updated) <= target_tokens:
        return updated

    # Stage 2: history trimming.
    if isinstance(updated.get("messages"), list):
        updated["messages"] = _trim_messages(updated["messages"], keep_last=keep_recent_messages)
    if _body_tokens(updated) <= target_tokens:
        return updated

    # Stage 3: system-prompt marker fallback.  This is intentionally heavy
    # — by stage 3 the user has lost most of the conversation context
    # anyway, so dropping the harness system prompt lets the request at
    # least reach the upstream.
    if "/messages" in str(updated.get("_path_hint", "")) or isinstance(updated.get("system"), (str, list)):
        updated["system"] = "[headroom] system prompt omitted: upstream request size cap exceeded"
    elif isinstance(updated.get("messages"), list):
        # OpenAI Chat / Responses path: collapse system message to a marker.
        new_messages = []
        for msg in updated["messages"]:
            if isinstance(msg, dict) and msg.get("role") == "system":
                new_messages.append({"role": "system", "content": "[headroom] system prompt omitted: upstream request size cap exceeded"})
            else:
                new_messages.append(msg)
        updated["messages"] = new_messages
    return updated


def compress_to_upstream_cap(body: Json, *, max_bytes: int) -> Json:
    """Convenience wrapper: pick a token target from a byte budget and compress.

    Uses 1 token ≈ 3 bytes (English-leaning; CJK is denser) so the cap
    stays on the safe side for relays that count raw bytes.
    """
    if max_bytes <= 0:
        return body
    target_tokens = max(512, max_bytes // 3)
    return headroom_compress(body, target_tokens=target_tokens)
