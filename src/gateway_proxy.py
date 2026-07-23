#!/usr/bin/env python3
"""Native proxy client for forwarding requests to upstream API.

Handles HTTP requests to the upstream LLM API with proper error handling and retry logic.
"""
from __future__ import annotations

import json
import os
import email.utils
import random
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from collections.abc import Iterator
from typing import Any

Json = dict[str, Any]

from .gateway_errors import GatewayError, UpstreamHTTPError, UpstreamTimeoutError
from .gateway_process_ops import run_bounded_process

# Only statuses that are normally safe to retry before any downstream output
# or tool side effect has been produced.
_RETRY_STATUSES = {429, 502, 503, 504}
_SAFE_RESPONSE_HEADERS = {"content-type", "retry-after", "request-id", "x-request-id"}


@dataclass(frozen=True)
class UpstreamSSEEvent:
    event: str | None
    data: str
    raw_bytes: int


def _bounded_text(value: Any, limit: int = 2000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _curl_header_config(headers: dict[str, str]) -> bytes:
    """Build curl stdin config so credentials never appear in process argv."""
    lines: list[str] = []
    for raw_key, raw_value in headers.items():
        key = str(raw_key).strip()
        value = str(raw_value)
        if not key or any(char in key for char in "\r\n:") or any(char in value for char in "\r\n"):
            raise UpstreamHTTPError(
                502,
                {"type": "invalid_upstream_header", "header": _bounded_text(key, 100)},
            )
        escaped = f"{key}: {value}".replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'header = "{escaped}"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def _read_curl_headers(path: str | None) -> dict[str, str]:
    """Read the final response header block emitted by curl, safely bounded."""
    if not path:
        return {}
    try:
        with open(path, "rb") as handle:
            raw = handle.read(64 * 1024).decode("iso-8859-1", errors="replace")
    except OSError:
        return {}
    blocks = [block for block in raw.replace("\r\n", "\n").split("\n\n") if block.lstrip().startswith("HTTP/")]
    if not blocks:
        return {}
    headers: dict[str, str] = {}
    for line in blocks[-1].splitlines()[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().lower()
        if normalized in _SAFE_RESPONSE_HEADERS:
            headers[normalized] = _bounded_text(value.strip(), 1000)
    return headers


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    raw = str(headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


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
        profile: Json | None = None,
        _pool: Any | None = None,
        _allow_failover: bool = True,
    ) -> None:
        from .gateway_config import _upstream_config

        explicit_transport = base_url is not None or api_key is not None or model is not None
        selected_profile = dict(profile) if isinstance(profile, dict) else None
        pool = _pool
        if selected_profile is None and not explicit_transport:
            try:
                from .gateway_upstream_pool import get_upstream_pool

                pool = pool or get_upstream_pool()
                selected_profile = pool.select_profile()
            except Exception:
                selected_profile = None
                pool = None
        cfg = selected_profile or _upstream_config()
        self._cfg = dict(cfg)
        self.base_url = (base_url or cfg.get("base_url", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else cfg.get("api_key", "")
        self.model = model if model is not None else cfg.get("model", "")
        self.profile_id = str(cfg.get("id") or cfg.get("name") or "active")
        self._pool = pool
        self._allow_failover = bool(_allow_failover and pool is not None and not explicit_transport)
        self.paths = dict(cfg.get("paths")) if isinstance(cfg.get("paths"), dict) else {}
        self.timeout = max(0.1, float(cfg.get("timeout_seconds", 60.0) or 60.0))
        self.retry_max_attempts = max(1, int(cfg.get("retry_max_attempts", 3) or 3))
        self.retry_initial_delay = max(0.0, float(cfg.get("retry_initial_delay_seconds", 0.5) or 0.0))
        self.retry_max_delay = max(self.retry_initial_delay, float(cfg.get("retry_max_delay_seconds", 4.0) or 0.0))
        self.retry_max_elapsed = max(
            0.1,
            float(cfg.get("retry_max_elapsed_seconds", max(self.timeout, 90.0)) or max(self.timeout, 90.0)),
        )
        self.max_response_bytes = max(1024, int(cfg.get("max_response_bytes", 32 * 1024 * 1024) or 32 * 1024 * 1024))
        self.max_stderr_bytes = max(1024, int(cfg.get("max_stderr_bytes", 256 * 1024) or 256 * 1024))
        self.max_stream_event_bytes = max(1024, int(cfg.get("max_stream_event_bytes", 1024 * 1024) or 1024 * 1024))
        self.max_stream_events = max(1, int(cfg.get("max_stream_events", 100_000) or 100_000))
        capabilities = cfg.get("capabilities") if isinstance(cfg.get("capabilities"), dict) else {}
        self.supports_streaming = bool(capabilities.get("supports_streaming", True))
        self.max_input_tokens = max(0, int(cfg.get("max_input_tokens", 0) or 0))
        env_protocol = os.environ.get("GATEWAY_UPSTREAM_PROTOCOL") or os.environ.get("UPSTREAM_PROTOCOL")
        self.protocol = str(env_protocol or cfg.get("protocol") or "openai_chat")
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
        try:
            from .gateway_observability import current_request_id
            request_id = current_request_id()
            if request_id:
                headers["x-request-id"] = request_id
        except Exception:
            pass
        return headers

    def _url(self, path: str) -> str:
        if "/chat/completions" in path:
            configured_path = self.paths.get("chat_completions", "/v1/chat/completions")
        elif "/responses" in path:
            configured_path = self.paths.get("responses", "/v1/responses")
        elif "/messages" in path:
            configured_path = self.paths.get("messages", "/v1/messages")
        elif "/models" in path:
            configured_path = self.paths.get("models", "/v1/models")
        else:
            configured_path = path
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

    def _do_request_once(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
        *,
        timeout: float | None = None,
    ) -> Json:
        # Use curl subprocess for better compatibility with non-standard servers
        import tempfile

        # Build curl command
        attempt_timeout = max(0.1, float(timeout if timeout is not None else self.timeout))
        cmd = [
            "curl", "-sS", "-X", method, url,
            "--connect-timeout", str(min(attempt_timeout, 10.0)),
            "--max-time", str(attempt_timeout),
            "--max-filesize", str(self.max_response_bytes),
            "--config", "-",
            "-w", "\n__HTTP_CODE__%{http_code}",
        ]
        curl_config = _curl_header_config(headers)

        temp_file = None
        header_file = None
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".headers") as header_handle:
            header_file = header_handle.name
        cmd.extend(["--dump-header", header_file])
        if data:
            # Write data to temp file for large payloads
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.json') as f:
                f.write(data)
                temp_file = f.name
            cmd.extend(["--data-binary", f"@{temp_file}"])

        try:
            result = run_bounded_process(
                cmd,
                input_data=curl_config,
                timeout=attempt_timeout + 1.0,
                stdout_limit=self.max_response_bytes + 4096,
                stderr_limit=self.max_stderr_bytes,
            )

            output = result.stdout
            response_headers = _read_curl_headers(header_file)

            if result.returncode != 0:
                if result.returncode == 63 or result.stdout_truncated:
                    raise UpstreamHTTPError(
                        502,
                        {
                            "type": "upstream_response_too_large",
                            "max_bytes": self.max_response_bytes,
                            "received_bytes": result.stdout_total_bytes,
                        },
                        headers=response_headers,
                    )
                detail = {
                    "type": "curl_transport_error",
                    "curl_exit_code": int(result.returncode),
                    "stderr": _bounded_text(result.stderr),
                    "stderr_truncated": bool(result.stderr_truncated),
                }
                if result.returncode == 28:
                    raise UpstreamTimeoutError(
                        f"upstream request timeout after {attempt_timeout:g}s",
                        detail=detail,
                    )
                raise UpstreamHTTPError(502, detail, headers=response_headers)

            # Extract HTTP status code
            if "\n__HTTP_CODE__" in output:
                response_data, status_line = output.rsplit("\n__HTTP_CODE__", 1)
                response_size = len(response_data.encode("utf-8"))
                if response_size > self.max_response_bytes:
                    raise UpstreamHTTPError(
                        502,
                        {
                            "type": "upstream_response_too_large",
                            "max_bytes": self.max_response_bytes,
                            "received_bytes": response_size,
                        },
                        headers=response_headers,
                    )
                try:
                    status_code = int(status_line.strip())
                except ValueError as exc:
                    raise UpstreamHTTPError(
                        502,
                        {"type": "invalid_upstream_status", "raw_status": _bounded_text(status_line, 100)},
                        headers=response_headers,
                    ) from exc
            else:
                raise UpstreamHTTPError(
                    502,
                    {"type": "missing_upstream_status_marker", "body_preview": _bounded_text(output)},
                    headers=response_headers,
                )

            if status_code == 0:
                raise UpstreamHTTPError(
                    502,
                    {"type": "upstream_connection_failed", "stderr": _bounded_text(result.stderr)},
                    headers=response_headers,
                )

            if status_code >= 400:
                try:
                    error_detail = json.loads(response_data) if response_data else {}
                except json.JSONDecodeError:
                    error_detail = {"raw": _bounded_text(response_data)}
                raise UpstreamHTTPError(status_code, error_detail, headers=response_headers)

            if response_data:
                if response_data.lstrip().startswith("data:"):
                    return self._aggregate_sse_response(response_data)
                try:
                    return json.loads(response_data)
                except json.JSONDecodeError:
                    return {"text": response_data}
            if status_code == 204:
                return {}
            raise UpstreamHTTPError(
                502,
                {"type": "empty_upstream_response", "upstream_status": status_code},
                headers=response_headers,
            )

        except subprocess.TimeoutExpired:
            raise UpstreamTimeoutError(
                f"upstream request timeout after {attempt_timeout:g}s",
                detail={"type": "subprocess_timeout"},
            )
        except Exception as e:
            if isinstance(e, GatewayError):
                raise
            raise UpstreamHTTPError(
                502,
                {"type": "upstream_transport_exception", "message": _bounded_text(e)},
            ) from e
        finally:
            for path_to_remove in (temp_file, header_file):
                if not path_to_remove:
                    continue
                try:
                    os.unlink(path_to_remove)
                except OSError:
                    pass

    def _do_request_impl(self, method: str, path: str, body: Json | None = None) -> Json:
        url = self._url(path)
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        deadline = time.monotonic() + self.retry_max_elapsed
        attempt = 0

        while attempt < self.retry_max_attempts:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpstreamTimeoutError(
                    f"upstream retry deadline exceeded after {self.retry_max_elapsed:g}s",
                    detail={"attempts": attempt - 1},
                )
            try:
                return self._do_request_once(
                    method,
                    url,
                    headers,
                    data,
                    timeout=min(self.timeout, remaining),
                )
            except UpstreamHTTPError as exc:
                last_error: GatewayError = exc
                retryable = exc.upstream_status in _RETRY_STATUSES
                retry_after = _retry_after_seconds(exc.headers)
            except UpstreamTimeoutError as exc:
                last_error = exc
                retryable = True
                retry_after = None

            if not retryable or attempt >= self.retry_max_attempts:
                raise last_error

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise last_error
            base_delay = min(
                self.retry_max_delay,
                self.retry_initial_delay * (2 ** (attempt - 1)),
            )
            delay = retry_after if retry_after is not None else base_delay * random.uniform(0.8, 1.2)
            if delay >= remaining:
                raise last_error
            if delay > 0:
                time.sleep(delay)

        raise UpstreamHTTPError(502, {"type": "retry_exhausted", "attempts": attempt})

    def _do_request(self, method: str, path: str, body: Json | None = None) -> Json:
        from .gateway_observability import observe_upstream
        started = time.monotonic()
        pool = getattr(self, "_pool", None)
        profile_id = str(getattr(self, "profile_id", "active") or "active")
        pool_started = pool.request_start(profile_id) if pool is not None else started
        try:
            result = self._do_request_impl(method, path, body)
        except Exception as exc:
            if pool is not None:
                pool.request_failure(profile_id, exc)
            observe_upstream(
                method=method,
                path=path,
                protocol=self.protocol,
                stream=False,
                success=False,
                failure_type=exc.__class__.__name__,
                duration_seconds=time.monotonic() - started,
            )
            raise
        finally:
            if pool is not None:
                pool.request_end(profile_id)
        if pool is not None:
            pool.request_success(profile_id, pool_started)
        observe_upstream(
            method=method,
            path=path,
            protocol=self.protocol,
            stream=False,
            success=True,
            failure_type="none",
            duration_seconds=time.monotonic() - started,
        )
        return result

    @staticmethod
    def _parse_sse_block(block: bytes) -> UpstreamSSEEvent | None:
        event_name: str | None = None
        data_lines: list[str] = []
        for raw_line in block.decode("utf-8", errors="replace").splitlines():
            if not raw_line or raw_line.startswith(":"):
                continue
            if raw_line.startswith("event:"):
                event_name = raw_line[6:].strip() or None
            elif raw_line.startswith("data:"):
                data_lines.append(raw_line[5:].lstrip())
        if not data_lines:
            return None
        return UpstreamSSEEvent(event_name, "\n".join(data_lines), len(block))

    def _stream_impl(self, path: str, body: Json) -> Iterator[UpstreamSSEEvent]:
        """Yield bounded upstream SSE events and close promptly on cancellation."""
        stream_body = dict(body)
        stream_body["stream"] = True
        data = json.dumps(stream_body, ensure_ascii=False).encode("utf-8")
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        request = urllib.request.Request(self._url(path), data=data, headers=headers, method="POST")
        response = None
        total_bytes = 0
        event_count = 0
        try:
            deadline = time.monotonic() + self.retry_max_elapsed
            attempt = 0
            while True:
                attempt += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamTimeoutError(
                        "upstream streaming retry deadline exceeded",
                        detail={"attempts": attempt - 1},
                    )
                try:
                    response = self._opener.open(request, timeout=min(self.timeout, remaining))
                    break
                except urllib.error.HTTPError as exc:
                    retryable = exc.code in _RETRY_STATUSES
                    retry_after = _retry_after_seconds({key.lower(): value for key, value in dict(exc.headers or {}).items()})
                    if not retryable or attempt >= self.retry_max_attempts:
                        raise
                    try:
                        exc.close()
                    except Exception:
                        pass
                except urllib.error.URLError:
                    retry_after = None
                    if attempt >= self.retry_max_attempts:
                        raise
                remaining = deadline - time.monotonic()
                base_delay = min(self.retry_max_delay, self.retry_initial_delay * (2 ** (attempt - 1)))
                delay = retry_after if retry_after is not None else base_delay * random.uniform(0.8, 1.2)
                if delay >= remaining:
                    raise UpstreamTimeoutError(
                        "upstream streaming retry deadline exceeded",
                        detail={"attempts": attempt},
                    )
                if delay > 0:
                    time.sleep(delay)
            content_type = str(response.headers.get("content-type") or "").lower()
            if "text/event-stream" not in content_type:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise UpstreamHTTPError(
                        502,
                        {
                            "type": "upstream_response_too_large",
                            "max_bytes": self.max_response_bytes,
                            "received_bytes": len(raw),
                        },
                    )
                yield UpstreamSSEEvent(None, raw.decode("utf-8", errors="replace"), len(raw))
                return

            block = bytearray()
            response_iterator = iter(response)
            while True:
                readline = getattr(response, "readline", None)
                line = readline(self.max_stream_event_bytes + 1) if callable(readline) else next(response_iterator, b"")
                if not line:
                    break
                if len(line) > self.max_stream_event_bytes:
                    raise UpstreamHTTPError(
                        502,
                        {
                            "type": "upstream_stream_event_too_large",
                            "max_bytes": self.max_stream_event_bytes,
                            "received_bytes": len(line),
                        },
                    )
                total_bytes += len(line)
                if total_bytes > self.max_response_bytes:
                    raise UpstreamHTTPError(
                        502,
                        {
                            "type": "upstream_stream_too_large",
                            "max_bytes": self.max_response_bytes,
                            "received_bytes": total_bytes,
                        },
                    )
                block.extend(line)
                if len(block) > self.max_stream_event_bytes:
                    raise UpstreamHTTPError(
                        502,
                        {
                            "type": "upstream_stream_event_too_large",
                            "max_bytes": self.max_stream_event_bytes,
                            "received_bytes": len(block),
                        },
                    )
                if line not in {b"\n", b"\r\n"}:
                    continue
                parsed = self._parse_sse_block(bytes(block))
                block.clear()
                if parsed is None:
                    continue
                event_count += 1
                if event_count > self.max_stream_events:
                    raise UpstreamHTTPError(
                        502,
                        {
                            "type": "upstream_stream_too_many_events",
                            "max_events": self.max_stream_events,
                        },
                    )
                yield parsed
            if block:
                parsed = self._parse_sse_block(bytes(block))
                if parsed is not None:
                    event_count += 1
                    if event_count > self.max_stream_events:
                        raise UpstreamHTTPError(
                            502,
                            {
                                "type": "upstream_stream_too_many_events",
                                "max_events": self.max_stream_events,
                            },
                        )
                    yield parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read(2000)
            try:
                detail = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                detail = {"raw": _bounded_text(raw.decode("utf-8", errors="replace"))}
            raise UpstreamHTTPError(exc.code, detail, headers=dict(exc.headers or {})) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise UpstreamTimeoutError("upstream streaming request timed out", detail={"type": "stream_timeout"}) from exc
            raise UpstreamHTTPError(502, {"type": "upstream_stream_connection_failed", "message": _bounded_text(exc.reason)}) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamTimeoutError("upstream streaming request timed out", detail={"type": "stream_timeout"}) from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def stream(self, path: str, body: Json) -> Iterator[UpstreamSSEEvent]:
        """Observe total and first-event latency around the bounded SSE transport."""
        from .gateway_observability import observe_upstream
        started = time.monotonic()
        pool = getattr(self, "_pool", None)
        profile_id = str(getattr(self, "profile_id", "active") or "active")
        pool_started = pool.request_start(profile_id) if pool is not None else started
        first_event_seconds: float | None = None
        event_count = 0
        success = False
        failure_type = "client_disconnect"
        try:
            for event in self._stream_impl(path, body):
                event_count += 1
                if first_event_seconds is None:
                    first_event_seconds = time.monotonic() - started
                yield event
            success = True
            failure_type = "none"
        except GeneratorExit:
            raise
        except Exception as exc:
            failure_type = exc.__class__.__name__
            if pool is not None:
                pool.request_failure(profile_id, exc)
            raise
        finally:
            if pool is not None:
                if success:
                    pool.request_success(profile_id, pool_started)
                pool.request_end(profile_id)
            observe_upstream(
                method="POST",
                path=path,
                protocol=self.protocol,
                stream=True,
                success=success,
                failure_type=failure_type,
                duration_seconds=time.monotonic() - started,
                first_event_seconds=first_event_seconds,
                event_count=event_count,
            )

    @staticmethod
    def _pool_retryable(exc: BaseException) -> bool:
        if isinstance(exc, UpstreamTimeoutError):
            return True
        return isinstance(exc, UpstreamHTTPError) and exc.upstream_status in _RETRY_STATUSES

    def _failover_clients(self) -> Iterator["NativeProxyClient"]:
        if not self._allow_failover or self._pool is None:
            return
        for profile in self._pool.failover_profiles(self.profile_id):
            yield NativeProxyClient(
                profile=profile,
                _pool=self._pool,
                _allow_failover=False,
            )

    def get(self, path: str) -> Json:
        try:
            return self._do_request("GET", path)
        except Exception as first_error:
            if not self._pool_retryable(first_error):
                raise
            last_error: BaseException = first_error
            for client in self._failover_clients():
                try:
                    return client._do_request("GET", path)
                except Exception as exc:
                    last_error = exc
                    if not self._pool_retryable(exc):
                        raise
            raise last_error

    def get_upstream_path(self, path: str) -> Json:
        return self.get(path)

    def post(self, path: str, body: Json) -> Json:
        try:
            return self._do_request("POST", path, body)
        except Exception as first_error:
            if not self._pool_retryable(first_error):
                raise
            last_error: BaseException = first_error
            for client in self._failover_clients():
                try:
                    return client._do_request("POST", path, body)
                except Exception as exc:
                    last_error = exc
                    if not self._pool_retryable(exc):
                        raise
            raise last_error

    def _forward_once(self, path: str, body: Json) -> Json:
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
        max_tokens = self.max_input_tokens or _headroom_max_input_tokens()
        if max_tokens > 0:
            upstream_body = headroom_compress(upstream_body, target_tokens=max_tokens)
        response = self._do_request("POST", upstream_path, upstream_body)
        return _convert_response_to_downstream(path, response, self.protocol)

    def forward(self, path: str, body: Json) -> Json:
        try:
            return self._forward_once(path, body)
        except Exception as first_error:
            if not self._pool_retryable(first_error):
                raise
            last_error: BaseException = first_error
            for client in self._failover_clients():
                try:
                    return client._forward_once(path, body)
                except Exception as exc:
                    last_error = exc
                    if not self._pool_retryable(exc):
                        raise
            raise last_error
