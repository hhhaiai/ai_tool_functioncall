#!/usr/bin/env python3
"""Native proxy client for forwarding requests to upstream API.

Handles HTTP requests to the upstream LLM API with proper error handling and retry logic.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

Json = dict[str, Any]

from .gateway_errors import UpstreamHTTPError

# Retry configuration for transient upstream errors (502/503/504)
_RETRY_STATUSES = {502, 503, 504}
_RETRY_INTERVAL_SECONDS = 30
_RETRY_MAX_SECONDS = 20 * 60  # 20 minutes


class NativeProxyClient:
    # Shared opener pool for connection reuse across instances
    _opener_cache: dict[str, urllib.request.OpenerDirector] = {}
    _opener_lock = __import__("threading").Lock()

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        from .gateway_config import _upstream_config, _upstream_protocol
        cfg = _upstream_config()
        self.base_url = (base_url or cfg.get("base_url", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else cfg.get("api_key", "")
        self.model = model if model is not None else cfg.get("model", "")
        self.timeout = cfg.get("timeout_seconds", 60.0)
        self.protocol = _upstream_protocol()
        self._opener = self._get_opener()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            if self.protocol == "anthropic_messages":
                headers["x-api-key"] = self.api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _url(self, path: str) -> str:
        from .gateway_config import _configured_upstream_path
        configured_path = _configured_upstream_path(path)
        return f"{self.base_url}{configured_path}"

    def _aggregate_sse_response(self, response_data: str) -> Json:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        msg_id = "chatcmpl_gateway_stream"
        model = ""
        for raw_line in response_data.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            msg_id = payload.get("id") or msg_id
            model = payload.get("model") or model
            for choice in payload.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning"):
                    reasoning_parts.append(str(delta.get("reasoning")))
                if delta.get("content"):
                    content_parts.append(str(delta.get("content")))
        return {
            "id": msg_id,
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "".join(content_parts), "reasoning": "".join(reasoning_parts)},
                "finish_reason": "stop",
            }],
        }

    @classmethod
    def _get_opener(cls) -> urllib.request.OpenerDirector:
        """Get or create a shared opener with connection reuse."""
        import socket
        key = "default"
        with cls._opener_lock:
            if key not in cls._opener_cache:
                opener = urllib.request.build_opener()
                cls._opener_cache[key] = opener
            return cls._opener_cache[key]

    def _do_request_once(self, method: str, url: str, headers: dict[str, str], data: bytes | None) -> Json:
        # Use curl subprocess for better compatibility with non-standard servers
        import subprocess
        import tempfile

        # Build curl command
        cmd = ["curl", "-s", "-X", method, url, "-w", "\n__HTTP_CODE__%{http_code}"]
        for key, value in headers.items():
            cmd.extend(["-H", f"{key}: {value}"])

        if data:
            # Write data to temp file for large payloads
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.json') as f:
                f.write(data)
                temp_file = f.name
            cmd.extend(["--data-binary", f"@{temp_file}"])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.timeout,
                check=False
            )

            if data:
                try:
                    os.unlink(temp_file)
                except:
                    pass

            output = result.stdout.decode('utf-8')

            # Extract HTTP status code
            if "\n__HTTP_CODE__" in output:
                response_data, status_line = output.rsplit("\n__HTTP_CODE__", 1)
                status_code = int(status_line.strip())
            else:
                response_data = output
                status_code = 200

            if status_code >= 400:
                from .gateway_errors import UpstreamHTTPError
                try:
                    error_detail = json.loads(response_data) if response_data else {}
                except:
                    error_detail = {"raw": response_data}
                raise UpstreamHTTPError(
                    f"upstream returned {status_code}",
                    upstream_status=status_code,
                    detail=error_detail
                )

            if response_data:
                if response_data.lstrip().startswith("data:"):
                    return self._aggregate_sse_response(response_data)
                try:
                    return json.loads(response_data)
                except json.JSONDecodeError:
                    return {"text": response_data}
            return {}

        except subprocess.TimeoutExpired:
            from .gateway_errors import GatewayError
            raise GatewayError(f"upstream request timeout after {self.timeout}s")
        except Exception as e:
            if "UpstreamHTTPError" in type(e).__name__ or "GatewayError" in type(e).__name__:
                raise
            from .gateway_errors import GatewayError
            raise GatewayError(f"upstream request failed: {e}") from e

    def _do_request(self, method: str, path: str, body: Json | None = None) -> Json:
        url = self._url(path)
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        deadline = time.monotonic() + _RETRY_MAX_SECONDS
        last_error: Exception | None = None

        while True:
            try:
                return self._do_request_once(method, url, headers, data)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                last_error = UpstreamHTTPError(e.code, detail)
                if e.code not in _RETRY_STATUSES or time.monotonic() >= deadline:
                    raise last_error
            except urllib.error.URLError as e:
                last_error = UpstreamHTTPError(502, str(e.reason))
                if time.monotonic() >= deadline:
                    raise last_error

            time.sleep(_RETRY_INTERVAL_SECONDS)

    def get(self, path: str) -> Json:
        return self._do_request("GET", path)

    def get_upstream_path(self, path: str) -> Json:
        return self._do_request("GET", path)

    def post(self, path: str, body: Json) -> Json:
        return self._do_request("POST", path, body)

    def forward(self, path: str, body: Json) -> Json:
        from .gateway_config import _force_upstream_stream_aggregate, _headroom_max_input_tokens
        from .gateway_protocol import _convert_request_to_upstream, _convert_response_to_downstream
        from .gateway_headroom import headroom_compress
        upstream_path, upstream_body = _convert_request_to_upstream(path, body, self.protocol)
        if _force_upstream_stream_aggregate():
            upstream_body = dict(upstream_body)
            upstream_body["stream"] = True
        # Headroom-style progressive compression: weak upstream relays (e.g.
        # anthropic -> openai -> mimo chains) cap request size well below the
        # advertised window.  If the body exceeds the configured per-request
        # cap, layer transforms (tool result crushing, history trim, system
        # prompt marker) until we fit.  This keeps the request alive instead
        # of the upstream returning a generic ``text too long`` refusal.
        max_tokens = _headroom_max_input_tokens()
        if max_tokens > 0:
            upstream_body = headroom_compress(upstream_body, target_tokens=max_tokens)
        response = self.post(upstream_path, upstream_body)
        return _convert_response_to_downstream(path, response, self.protocol)
