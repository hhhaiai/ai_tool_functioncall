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

from .gateway_errors import BadRequestError, GatewayError
from .gateway_http_actions import _http_action_opener, _validate_action_url

Json = dict[str, Any]
WEB2API_PATHS = {"/v1/web2api", "/api/web2api"}


class Web2APIError(GatewayError):
    status = 502


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
        allow_private_network: bool = False,
        max_cache_entries: int = 256,
    ):
        self.enabled = enabled
        self.max_concurrent = max_concurrent
        self.cache_ttl_seconds = cache_ttl_seconds
        self.request_timeout = request_timeout
        self.max_content_bytes = max_content_bytes
        self.user_agent = user_agent
        self.allow_private_network = bool(allow_private_network)
        self.max_cache_entries = max(1, int(max_cache_entries))

        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()
        self._semaphore = threading.Semaphore(max_concurrent)
        self._stats_lock = threading.Lock()

        # Stats
        self._requests = 0
        self._cache_hits = 0
        self._errors = 0

    @property
    def stats(self) -> dict[str, Any]:
        """Get engine statistics."""
        with self._cache_lock, self._stats_lock:
            return {
                "enabled": self.enabled,
                "requests": self._requests,
                "cache_hits": self._cache_hits,
                "errors": self._errors,
                "cache_entries": len(self._cache),
                "max_cache_entries": self.max_cache_entries,
                "allow_private_network": self.allow_private_network,
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
                    with self._stats_lock:
                        self._cache_hits += 1
                    return dict(result)
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
            while len(self._cache) > self.max_cache_entries:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                del self._cache[oldest]

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

        with self._stats_lock:
            self._requests += 1

        # Validate URL
        action = {"allow_private_network": self.allow_private_network}
        try:
            _validate_action_url(url, action)
        except Exception as exc:
            with self._stats_lock:
                self._errors += 1
            return {
                "error": str(exc),
                "failure_type": "invalid_url",
                "success": False,
            }

        # Acquire semaphore for concurrency control
        acquired = self._semaphore.acquire(timeout=max(0.1, float(self.request_timeout)))
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

            with _http_action_opener(action).open(req, timeout=self.request_timeout) as resp:
                # Check content length
                content_length = resp.headers.get("Content-Length")
                try:
                    declared_length = int(content_length) if content_length else 0
                except (TypeError, ValueError):
                    declared_length = 0
                if declared_length > self.max_content_bytes:
                    raise Web2APIError(
                        f"Content too large: {declared_length} bytes",
                        detail={"max_content_bytes": self.max_content_bytes},
                    )
                content_type = str(resp.headers.get("Content-Type", ""))
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type and not (
                    media_type.startswith("text/")
                    or media_type in {"application/xhtml+xml", "application/xml", "application/json"}
                    or media_type.endswith("+xml")
                ):
                    raise Web2APIError(
                        f"Unsupported content type: {media_type}",
                        detail={"content_type": media_type},
                    )
                html_bytes = resp.read(self.max_content_bytes + 1)
                if len(html_bytes) > self.max_content_bytes:
                    raise Web2APIError(
                        f"Content exceeded max_content_bytes={self.max_content_bytes}",
                        detail={"max_content_bytes": self.max_content_bytes},
                    )
                html = html_bytes.decode("utf-8", errors="replace")

                return {
                    "url": resp.url,  # Final URL after redirects
                    "status": resp.status,
                    "content_type": content_type,
                    "html": html,
                    "text": _extract_text_content(html),
                    "title": _extract_title(html),
                    "success": True,
                }

        except urllib.error.HTTPError as e:
            with self._stats_lock:
                self._errors += 1
            return {"error": f"HTTP {e.code}: {e.reason}", "failure_type": "http_error", "success": False}
        except urllib.error.URLError as e:
            with self._stats_lock:
                self._errors += 1
            return {"error": f"URL error: {e.reason}", "failure_type": "transport_error", "success": False}
        except Exception as e:
            with self._stats_lock:
                self._errors += 1
            return {"error": str(e), "failure_type": "fetch_failed", "success": False}
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
                    request_timeout=w2a_config.get("request_timeout", 30),
                    max_content_bytes=w2a_config.get("max_content_bytes", 5 * 1024 * 1024),
                    user_agent=str(w2a_config.get("user_agent") or "Gateway-Web2API/1.0"),
                    allow_private_network=bool(w2a_config.get("allow_private_network", False)),
                    max_cache_entries=w2a_config.get("max_cache_entries", 256),
                )

    return _engine


def reset_engine() -> None:
    """Reset the global engine (for testing)."""
    global _engine
    with _engine_lock:
        _engine = None


def _bounded_string_mapping(value: Any, *, name: str, max_fields: int, max_value_chars: int) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BadRequestError(f"{name} must be an object")
    if len(value) > max_fields:
        raise BadRequestError(f"{name} exceeds maximum field count {max_fields}")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        item = str(raw_value)
        if not key or len(key) > 128:
            raise BadRequestError(f"{name} contains an invalid field name")
        if not item or len(item) > max_value_chars:
            raise BadRequestError(f"{name}.{key} is empty or too long")
        result[key] = item
    return result


def execute_web2api_request(body: Json) -> Json:
    """Validate and execute one authenticated HTTP Web2API request."""
    from .gateway_config import load_config

    cfg = load_config()
    raw = cfg.get("web2api") if isinstance(cfg.get("web2api"), dict) else {}
    if not bool(raw.get("enabled", True)):
        raise Web2APIError("Web2API is disabled", detail={"enabled": False})
    url = str(body.get("url") or "").strip()
    if not url:
        raise BadRequestError("missing required field: url")
    mode = str(body.get("extraction_mode") or "auto").strip().lower()
    if mode not in {"auto", "css", "regex"}:
        raise BadRequestError("extraction_mode must be auto, css, or regex")
    selectors = _bounded_string_mapping(
        body.get("selectors"),
        name="selectors",
        max_fields=50,
        max_value_chars=256,
    )
    regex_patterns = _bounded_string_mapping(
        body.get("regex_patterns"),
        name="regex_patterns",
        max_fields=20,
        max_value_chars=512,
    )
    if regex_patterns and not bool(raw.get("allow_regex", False)):
        raise BadRequestError("regex extraction is disabled by the operator")
    include_raw_html = bool(body.get("include_raw_html", False))
    if include_raw_html and not bool(raw.get("allow_raw_html", False)):
        raise BadRequestError("raw HTML responses are disabled by the operator")
    result = get_web2api_engine().extract_structured(
        url,
        selectors=selectors or None,
        regex_patterns=regex_patterns or None,
        extraction_mode=mode,
        include_raw_html=include_raw_html,
        include_links=bool(body.get("include_links", False)),
    )
    if not result.get("success"):
        failure_type = str(result.get("failure_type") or "fetch_failed")
        if failure_type == "invalid_url":
            raise BadRequestError(str(result.get("error") or "invalid Web2API URL"), detail=result)
        raise Web2APIError(str(result.get("error") or "Web2API fetch failed"), detail=result)
    return {"object": "gateway.web2api.result", **result}


__all__ = [
    "SimpleHTMLExtractor",
    "WEB2API_PATHS",
    "Web2APIError",
    "Web2ApiEngine",
    "execute_web2api_request",
    "get_web2api_engine",
    "reset_engine",
]
