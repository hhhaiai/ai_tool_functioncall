"""Web2API engine for the gateway.

Wraps web pages into structured API responses, allowing downstream
clients to extract structured data from arbitrary web pages.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional
from html.parser import HTMLParser

Json = dict[str, Any]


class SimpleHTMLExtractor(HTMLParser):
    """Simple HTML parser for CSS selector-like extraction."""

    def __init__(self):
        super().__init__()
        self._elements: list[dict] = []
        self._current_tag = None
        self._current_attrs = {}
        self._current_data = []
        self._stack = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        self._stack.append({
            "tag": tag,
            "attrs": dict(attrs),
            "data": [],
        })

    def handle_endtag(self, tag: str):
        if self._stack and self._stack[-1]["tag"] == tag:
            element = self._stack.pop()
            element["text"] = "".join(element["data"]).strip()
            self._elements.append(element)

    def handle_data(self, data: str):
        if self._stack:
            self._stack[-1]["data"].append(data)

    def get_elements(self) -> list[dict]:
        return self._elements


def _simple_css_select(html: str, selector: str) -> list[str]:
    """Simple CSS selector implementation.

    Supports:
    - tag selectors: "p", "div", "h1"
    - class selectors: ".classname"
    - id selectors: "#idname"
    - tag.class: "p.intro"
    - tag#id: "div#main"
    """
    parser = SimpleHTMLExtractor()
    try:
        parser.feed(html)
    except Exception:
        return []

    elements = parser.get_elements()
    results = []

    # Parse selector
    tag = None
    class_name = None
    id_name = None

    if "." in selector:
        parts = selector.split(".", 1)
        tag = parts[0] if parts[0] else None
        class_name = parts[1]
    elif "#" in selector:
        parts = selector.split("#", 1)
        tag = parts[0] if parts[0] else None
        id_name = parts[1]
    else:
        tag = selector

    for elem in elements:
        elem_tag = elem.get("tag", "")
        elem_attrs = elem.get("attrs", {})

        # Match tag
        if tag and elem_tag != tag:
            continue

        # Match class
        if class_name:
            elem_class = elem_attrs.get("class", "")
            if class_name not in elem_class.split():
                continue

        # Match id
        if id_name:
            if elem_attrs.get("id") != id_name:
                continue

        text = elem.get("text", "")
        if text:
            results.append(text)

    return results


def _regex_extract(text: str, pattern: str) -> list[str]:
    """Extract text using regex pattern."""
    try:
        matches = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
        return [m if isinstance(m, str) else str(m) for m in matches]
    except re.error:
        return []


def _extract_meta_content(html: str, name: str) -> str:
    """Extract meta tag content by name or property."""
    patterns = [
        rf'<meta\s+name=["\']?{re.escape(name)}["\']?\s+content=["\']([^"\']+)["\']',
        rf'<meta\s+content=["\']([^"\']+)["\']?\s+name=["\']?{re.escape(name)}["\']',
        rf'<meta\s+property=["\']?{re.escape(name)}["\']?\s+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _extract_title(html: str) -> str:
    """Extract page title."""
    match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_links(html: str) -> list[dict[str, str]]:
    """Extract all links from HTML."""
    pattern = r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([^<]*)</a>'
    matches = re.findall(pattern, html, re.IGNORECASE)
    return [{"url": url, "text": text.strip()} for url, text in matches]


def _extract_text_content(html: str) -> str:
    """Extract plain text from HTML, removing tags."""
    # Remove script and style tags
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


class Web2ApiEngine:
    """Engine for converting web pages to structured API responses."""

    def __init__(
        self,
        enabled: bool = True,
        max_concurrent: int = 5,
        cache_ttl_seconds: int = 300,
        request_timeout: int = 30,
        max_content_bytes: int = 5 * 1024 * 1024,  # 5MB
        user_agent: str = "Gateway-Web2API/1.0",
    ):
        self.enabled = enabled
        self.max_concurrent = max_concurrent
        self.cache_ttl_seconds = cache_ttl_seconds
        self.request_timeout = request_timeout
        self.max_content_bytes = max_content_bytes
        self.user_agent = user_agent

        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()
        self._semaphore = threading.Semaphore(max_concurrent)

        # Stats
        self._requests = 0
        self._cache_hits = 0
        self._errors = 0

    @property
    def stats(self) -> dict[str, Any]:
        """Get engine statistics."""
        return {
            "enabled": self.enabled,
            "requests": self._requests,
            "cache_hits": self._cache_hits,
            "errors": self._errors,
            "cache_entries": len(self._cache),
        }

    def _make_cache_key(self, url: str, selectors: dict | None = None) -> str:
        """Create cache key from URL and selectors."""
        key_parts = [url]
        if selectors:
            key_parts.append(json.dumps(selectors, sort_keys=True))
        return hashlib.sha256(":".join(key_parts).encode()).hexdigest()[:32]

    def _get_cached(self, cache_key: str) -> Optional[dict]:
        """Get cached result if not expired."""
        with self._cache_lock:
            if cache_key in self._cache:
                timestamp, result = self._cache[cache_key]
                if time.time() - timestamp < self.cache_ttl_seconds:
                    self._cache_hits += 1
                    return result
                else:
                    del self._cache[cache_key]
        return None

    def _set_cached(self, cache_key: str, result: dict) -> None:
        """Cache a result."""
        with self._cache_lock:
            self._cache[cache_key] = (time.time(), result)

            # Evict old entries
            now = time.time()
            expired = [
                k for k, (ts, _) in self._cache.items()
                if now - ts > self.cache_ttl_seconds
            ]
            for k in expired:
                del self._cache[k]

    def fetch_page(self, url: str) -> dict[str, Any]:
        """Fetch a web page and return raw content.

        Returns dict with:
        - url: final URL after redirects
        - status: HTTP status code
        - content_type: Content-Type header
        - html: raw HTML content
        - text: extracted plain text
        """
        if not self.enabled:
            return {"error": "Web2API is disabled", "success": False}

        self._requests += 1

        # Validate URL
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"error": f"Invalid URL scheme: {parsed.scheme}", "success": False}

        # Acquire semaphore for concurrency control
        acquired = self._semaphore.acquire(timeout=30)
        if not acquired:
            return {"error": "Too many concurrent requests", "success": False}

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                },
            )

            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                # Check content length
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > self.max_content_bytes:
                    return {
                        "error": f"Content too large: {content_length} bytes",
                        "success": False,
                    }

                html_bytes = resp.read(self.max_content_bytes)
                html = html_bytes.decode("utf-8", errors="replace")

                return {
                    "url": resp.url,  # Final URL after redirects
                    "status": resp.status,
                    "content_type": resp.headers.get("Content-Type", ""),
                    "html": html,
                    "text": _extract_text_content(html),
                    "title": _extract_title(html),
                    "success": True,
                }

        except urllib.error.HTTPError as e:
            self._errors += 1
            return {"error": f"HTTP {e.code}: {e.reason}", "success": False}
        except urllib.error.URLError as e:
            self._errors += 1
            return {"error": f"URL error: {e.reason}", "success": False}
        except Exception as e:
            self._errors += 1
            return {"error": str(e), "success": False}
        finally:
            self._semaphore.release()

    def extract_with_selectors(self, html: str, selectors: dict[str, str]) -> dict[str, Any]:
        """Extract data from HTML using CSS selectors.

        Args:
            html: HTML content
            selectors: Dict mapping field names to CSS selectors

        Returns:
            Dict mapping field names to extracted values
        """
        results = {}
        for field_name, selector in selectors.items():
            values = _simple_css_select(html, selector)
            if len(values) == 1:
                results[field_name] = values[0]
            elif len(values) > 1:
                results[field_name] = values
            else:
                results[field_name] = None
        return results

    def extract_with_regex(self, html: str, patterns: dict[str, str]) -> dict[str, Any]:
        """Extract data from HTML using regex patterns.

        Args:
            html: HTML content
            patterns: Dict mapping field names to regex patterns

        Returns:
            Dict mapping field names to extracted values
        """
        results = {}
        for field_name, pattern in patterns.items():
            values = _regex_extract(html, pattern)
            if len(values) == 1:
                results[field_name] = values[0]
            elif len(values) > 1:
                results[field_name] = values
            else:
                results[field_name] = None
        return results

    def extract_structured(
        self,
        url: str,
        selectors: dict[str, str] | None = None,
        regex_patterns: dict[str, str] | None = None,
        extraction_mode: str = "css",
        include_raw_html: bool = False,
        include_links: bool = False,
    ) -> dict[str, Any]:
        """Fetch and extract structured data from a web page.

        Args:
            url: URL to fetch
            selectors: CSS selectors for extraction
            regex_patterns: Regex patterns for extraction
            extraction_mode: "css", "regex", or "auto"
            include_raw_html: Whether to include raw HTML in response
            include_links: Whether to extract all links

        Returns:
            Structured extraction result
        """
        # Check cache. Output-shaping options must be part of the key so a
        # previous compact extraction cannot satisfy a later request that asks
        # for links/raw HTML or a different extraction mode.
        cache_key = self._make_cache_key(url, {
            "selectors": selectors or {},
            "regex_patterns": regex_patterns or {},
            "extraction_mode": extraction_mode,
            "include_raw_html": include_raw_html,
            "include_links": include_links,
        })
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        # Fetch page
        page = self.fetch_page(url)
        if not page.get("success"):
            return page

        html = page["html"]

        # Extract data based on mode
        extracted = {}

        if extraction_mode in ("css", "auto") and selectors:
            extracted.update(self.extract_with_selectors(html, selectors))

        if extraction_mode in ("regex", "auto") and regex_patterns:
            extracted.update(self.extract_with_regex(html, regex_patterns))

        # Auto-extraction for common patterns
        if extraction_mode == "auto" and not selectors and not regex_patterns:
            extracted["title"] = page.get("title", "")
            extracted["description"] = _extract_meta_content(html, "description")
            extracted["og_title"] = _extract_meta_content(html, "og:title")
            extracted["og_description"] = _extract_meta_content(html, "og:description")
            extracted["og_image"] = _extract_meta_content(html, "og:image")

        # Build result
        result = {
            "url": page["url"],
            "title": page.get("title", ""),
            "status": page.get("status"),
            "extracted": extracted,
            "success": True,
        }

        if include_links:
            result["links"] = _extract_links(html)

        if include_raw_html:
            result["html"] = html
        else:
            # Include a snippet
            result["text_snippet"] = page.get("text", "")[:1000]

        # Cache result
        self._set_cached(cache_key, result)

        return result


# Global engine instance
_engine: Optional[Web2ApiEngine] = None
_engine_lock = threading.Lock()


def get_web2api_engine() -> Web2ApiEngine:
    """Get or create the global Web2API engine instance."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from .gateway_config import load_config
                config = load_config()
                w2a_config = config.get("web2api", {})

                _engine = Web2ApiEngine(
                    enabled=w2a_config.get("enabled", True),
                    max_concurrent=w2a_config.get("max_concurrent", 5),
                    cache_ttl_seconds=w2a_config.get("cache_ttl_seconds", 300),
                )

    return _engine


def reset_engine() -> None:
    """Reset the global engine (for testing)."""
    global _engine
    with _engine_lock:
        _engine = None
