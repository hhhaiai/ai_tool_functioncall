"""Concurrency optimization module for the gateway.

Provides connection pooling, load balancing, and multi-upstream support
for improved throughput and reliability.
"""
from __future__ import annotations

import asyncio
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.client import HTTPConnection, HTTPSConnection

Json = dict[str, Any]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConcurrencyConfig:
    """Configuration for concurrency optimization."""
    enabled: bool = True
    max_connections: int = 100
    max_connections_per_host: int = 10
    connection_timeout: float = 10.0
    read_timeout: float = 60.0
    retry_count: int = 2
    retry_delay: float = 1.0
    load_balance_strategy: str = "round_robin"  # round_robin, least_connections, random
    health_check_interval: float = 30.0
    health_check_timeout: float = 5.0


def _concurrency_config(raw: dict | None = None) -> ConcurrencyConfig:
    """Parse concurrency config from raw dict."""
    if not raw:
        return ConcurrencyConfig()
    return ConcurrencyConfig(
        enabled=raw.get("enabled", True),
        max_connections=raw.get("max_connections", 100),
        max_connections_per_host=raw.get("max_connections_per_host", 10),
        connection_timeout=raw.get("connection_timeout", 10.0),
        read_timeout=raw.get("read_timeout", 60.0),
        retry_count=raw.get("retry_count", 2),
        retry_delay=raw.get("retry_delay", 1.0),
        load_balance_strategy=raw.get("load_balance_strategy", "round_robin"),
        health_check_interval=raw.get("health_check_interval", 30.0),
        health_check_timeout=raw.get("health_check_timeout", 5.0),
    )


# ---------------------------------------------------------------------------
# Upstream Health Status
# ---------------------------------------------------------------------------

@dataclass
class UpstreamHealth:
    """Health status of an upstream server."""
    url: str
    is_healthy: bool = True
    last_check: float = 0.0
    consecutive_failures: int = 0
    response_time: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    active_connections: int = 0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 1.0
        return self.success_count / total


# ---------------------------------------------------------------------------
# Connection Pool
# ---------------------------------------------------------------------------

class ConnectionPool:
    """Thread-safe HTTP connection pool."""

    def __init__(self, config: ConcurrencyConfig):
        self._config = config
        self._connections: dict[str, list[HTTPConnection | HTTPSConnection]] = {}
        self._lock = threading.Lock()
        self._active_count: dict[str, int] = {}

    def _get_host_key(self, url: str) -> str:
        """Extract host:port from URL."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"

    def get_connection(self, url: str) -> HTTPConnection | HTTPSConnection:
        """Get a connection from the pool or create a new one."""
        host_key = self._get_host_key(url)

        with self._lock:
            # Check connection limit (applies to both pooled and new)
            active = self._active_count.get(host_key, 0)
            if active >= self._config.max_connections_per_host:
                raise ConnectionError(f"Connection limit reached for {host_key}")
            self._active_count[host_key] = active + 1

            # Check if we have available pooled connections
            if host_key in self._connections and self._connections[host_key]:
                conn = self._connections[host_key].pop()
                return conn

        # Create new connection
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme == "https":
            conn = HTTPSConnection(
                parsed.hostname,
                port=parsed.port or 443,
                timeout=self._config.connection_timeout,
            )
        else:
            conn = HTTPConnection(
                parsed.hostname,
                port=parsed.port or 80,
                timeout=self._config.connection_timeout,
            )

        return conn

    def release_connection(self, url: str, conn: HTTPConnection | HTTPSConnection | None = None):
        """Release a connection slot and optionally return connection to pool."""
        host_key = self._get_host_key(url)
        with self._lock:
            if host_key in self._active_count:
                self._active_count[host_key] = max(0, self._active_count[host_key] - 1)
            # Return connection to pool if provided
            if conn is not None:
                if host_key not in self._connections:
                    self._connections[host_key] = []
                if len(self._connections[host_key]) < self._config.max_connections_per_host:
                    self._connections[host_key].append(conn)
                else:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def return_connection(self, url: str, conn: HTTPConnection | HTTPSConnection | None = None):
        """Return a connection to the pool (backward-compatible alias)."""
        self.release_connection(url, conn)

    def close_all(self):
        """Close all connections in the pool."""
        with self._lock:
            for host_key, conns in self._connections.items():
                for conn in conns:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._connections.clear()
            self._active_count.clear()


# ---------------------------------------------------------------------------
# Load Balancer
# ---------------------------------------------------------------------------

class LoadBalancer:
    """Load balancer for multiple upstream servers."""

    def __init__(
        self,
        upstreams: list[dict[str, Any]],
        config: ConcurrencyConfig,
    ):
        self._config = config
        self._upstreams = upstreams
        self._health: dict[str, UpstreamHealth] = {}
        self._index = 0
        self._lock = threading.Lock()

        # Initialize health status for each upstream
        for upstream in upstreams:
            url = upstream.get("url", "")
            self._health[url] = UpstreamHealth(url=url)

    def get_next(self) -> dict[str, Any] | None:
        """Get the next upstream based on load balancing strategy."""
        healthy_upstreams = [
            u for u in self._upstreams
            if self._health.get(u.get("url", ""), UpstreamHealth(url="")).is_healthy
        ]

        if not healthy_upstreams:
            # Fall back to all upstreams if none are healthy
            healthy_upstreams = self._upstreams

        if not healthy_upstreams:
            return None

        strategy = self._config.load_balance_strategy

        with self._lock:
            if strategy == "round_robin":
                upstream = healthy_upstreams[self._index % len(healthy_upstreams)]
                self._index += 1
                return upstream

            elif strategy == "least_connections":
                # Return upstream with least active connections
                return min(
                    healthy_upstreams,
                    key=lambda u: self._health.get(
                        u.get("url", ""), UpstreamHealth(url="")
                    ).active_connections,
                )

            elif strategy == "random":
                import random
                return random.choice(healthy_upstreams)

            else:
                # Default to round robin
                upstream = healthy_upstreams[self._index % len(healthy_upstreams)]
                self._index += 1
                return upstream

    def report_success(self, url: str, response_time: float):
        """Report a successful request to an upstream."""
        with self._lock:
            if url in self._health:
                health = self._health[url]
                health.is_healthy = True
                health.consecutive_failures = 0
                health.response_time = response_time
                health.success_count += 1

    def report_failure(self, url: str):
        """Report a failed request to an upstream."""
        with self._lock:
            if url in self._health:
                health = self._health[url]
                health.consecutive_failures += 1
                health.failure_count += 1

                # Mark as unhealthy after 3 consecutive failures
                if health.consecutive_failures >= 3:
                    health.is_healthy = False

    def report_request_start(self, url: str):
        """Record that a request is currently using an upstream."""
        with self._lock:
            if url in self._health:
                self._health[url].active_connections += 1

    def report_request_end(self, url: str):
        """Record that a request stopped using an upstream."""
        with self._lock:
            if url in self._health:
                health = self._health[url]
                health.active_connections = max(0, health.active_connections - 1)

    def check_health(self, url: str) -> bool:
        """Check if an upstream is healthy."""
        if url not in self._health:
            return True

        with self._lock:
            health = self._health[url]

            # Skip check if recently checked
            if time.time() - health.last_check < self._config.health_check_interval:
                return health.is_healthy

            health.last_check = time.time()

        try:
            start = time.time()
            req = Request(url, method="HEAD")
            with urlopen(req, timeout=self._config.health_check_timeout) as resp:
                response_time = time.time() - start
                with self._lock:
                    health.response_time = response_time
                    health.is_healthy = True
                    health.consecutive_failures = 0
                return True
        except Exception:
            with self._lock:
                health.consecutive_failures += 1
                if health.consecutive_failures >= 3:
                    health.is_healthy = False
            return False

    def get_health_status(self) -> dict[str, dict]:
        """Get health status of all upstreams."""
        return {
            url: {
                "is_healthy": h.is_healthy,
                "response_time": h.response_time,
                "success_rate": h.success_rate,
                "consecutive_failures": h.consecutive_failures,
                "active_connections": h.active_connections,
            }
            for url, h in self._health.items()
        }


# ---------------------------------------------------------------------------
# Request Queue
# ---------------------------------------------------------------------------

@dataclass
class QueuedRequest:
    """A request queued for execution."""
    request_id: str
    url: str
    method: str
    headers: dict[str, str]
    body: bytes | None
    priority: int = 0  # Higher = more priority
    created_at: float = field(default_factory=time.time)
    callback: Callable | None = None


class RequestQueue:
    """Priority request queue with concurrency control."""

    def __init__(self, config: ConcurrencyConfig):
        self._config = config
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=config.max_connections)
        self._active_requests = 0
        self._semaphore = threading.Semaphore(config.max_connections)

    def enqueue(self, request: QueuedRequest) -> Future:
        """Enqueue a request for execution."""
        # Submit for execution directly (no unbounded queue)
        return self._executor.submit(self._execute_request, request)

    def _execute_request(self, request: QueuedRequest) -> dict:
        """Execute a queued request."""
        # Wait for semaphore (concurrency control)
        self._semaphore.acquire()

        try:
            with self._lock:
                self._active_requests += 1

            # Execute the request
            start = time.time()
            try:
                req = Request(
                    request.url,
                    data=request.body,
                    headers=request.headers,
                    method=request.method,
                )
                with urlopen(req, timeout=self._config.read_timeout) as resp:
                    response_time = time.time() - start
                    content = resp.read().decode("utf-8", errors="replace")

                    return {
                        "success": True,
                        "status_code": resp.status,
                        "content": content,
                        "response_time": response_time,
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": str(e),
                    "response_time": time.time() - start,
                }
        finally:
            with self._lock:
                self._active_requests -= 1
            self._semaphore.release()

    @property
    def queue_size(self) -> int:
        """Get current queue size (always 0 since requests are submitted directly)."""
        return 0

    @property
    def active_requests(self) -> int:
        """Get number of active requests."""
        with self._lock:
            return self._active_requests

    def shutdown(self, wait: bool = True):
        """Shutdown the request queue."""
        self._executor.shutdown(wait=wait)


# ---------------------------------------------------------------------------
# Concurrent Request Executor
# ---------------------------------------------------------------------------

class ConcurrentRequestExecutor:
    """Executes requests to multiple upstreams concurrently."""

    def __init__(
        self,
        upstreams: list[dict[str, Any]],
        config: ConcurrencyConfig | None = None,
    ):
        if config is None:
            config = ConcurrencyConfig()

        self._config = config
        self._load_balancer = LoadBalancer(upstreams, config)
        self._connection_pool = ConnectionPool(config)
        self._request_queue = RequestQueue(config)
        self._executor = ThreadPoolExecutor(max_workers=config.max_connections)

    def execute_single(
        self,
        path: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        """Execute a single request with retry and load balancing."""
        if headers is None:
            headers = {}

        last_error = None

        for attempt in range(self._config.retry_count + 1):
            # Get next upstream
            upstream = self._load_balancer.get_next()
            if not upstream:
                return {"success": False, "error": "No upstream available"}

            upstream_url = upstream.get("url", "")
            url = upstream_url + path
            self._load_balancer.report_request_start(upstream_url)

            try:
                start = time.time()
                req = Request(url, data=body, headers=headers, method=method)
                with urlopen(req, timeout=self._config.read_timeout) as resp:
                    response_time = time.time() - start
                    content = resp.read().decode("utf-8", errors="replace")

                    # Report success
                    self._load_balancer.report_success(upstream_url, response_time)

                    return {
                        "success": True,
                        "status_code": resp.status,
                        "content": content,
                        "response_time": response_time,
                        "upstream": upstream_url,
                    }

            except Exception as e:
                last_error = e
                self._load_balancer.report_failure(upstream_url)

                # Wait before retry
                if attempt < self._config.retry_count:
                    time.sleep(self._config.retry_delay * (attempt + 1))
            finally:
                self._load_balancer.report_request_end(upstream_url)

        return {
            "success": False,
            "error": str(last_error),
            "upstream": upstream.get("url", "") if upstream else None,
        }

    def execute_parallel(
        self,
        requests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Execute multiple requests in parallel."""
        futures = []

        for req in requests:
            future = self._executor.submit(
                self.execute_single,
                path=req.get("path", "/"),
                method=req.get("method", "POST"),
                headers=req.get("headers"),
                body=req.get("body"),
            )
            futures.append(future)

        results = []
        for future in as_completed(futures):
            try:
                result = future.result(timeout=self._config.read_timeout + 10)
                results.append(result)
            except Exception as e:
                results.append({"success": False, "error": str(e)})

        return results

    def execute_with_fallback(
        self,
        path: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        fallback_upstream: str | None = None,
    ) -> dict[str, Any]:
        """Execute with fallback to a specific upstream."""
        # Try primary execution
        result = self.execute_single(path, method, headers, body)

        if result.get("success"):
            return result

        # Try fallback
        if fallback_upstream:
            url = fallback_upstream + path
            try:
                start = time.time()
                req = Request(url, data=body, headers=headers or {}, method=method)
                with urlopen(req, timeout=self._config.read_timeout) as resp:
                    content = resp.read().decode("utf-8", errors="replace")
                    return {
                        "success": True,
                        "status_code": resp.status,
                        "content": content,
                        "response_time": time.time() - start,
                        "upstream": fallback_upstream,
                        "fallback": True,
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": str(e),
                    "upstream": fallback_upstream,
                    "fallback": True,
                }

        return result

    def get_health_status(self) -> dict[str, Any]:
        """Get health status of all components."""
        return {
            "upstreams": self._load_balancer.get_health_status(),
            "queue_size": self._request_queue.queue_size,
            "active_requests": self._request_queue.active_requests,
        }

    def shutdown(self):
        """Shutdown all components."""
        self._request_queue.shutdown(wait=False)
        self._connection_pool.close_all()
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Multi-Upstream Manager
# ---------------------------------------------------------------------------

class MultiUpstreamManager:
    """Manages multiple upstream configurations with failover."""

    def __init__(self, upstreams: list[dict[str, Any]], config: ConcurrencyConfig | None = None):
        self._upstreams = list(upstreams)  # Defensive copy
        self._config = config or ConcurrencyConfig()
        self._executor = ConcurrentRequestExecutor(self._upstreams, self._config)
        self._primary_index = 0

    @property
    def executor(self) -> ConcurrentRequestExecutor:
        """Get the concurrent request executor."""
        return self._executor

    def get_primary_upstream(self) -> dict[str, Any] | None:
        """Get the primary upstream."""
        if not self._upstreams:
            return None
        return self._upstreams[self._primary_index % len(self._upstreams)]

    def switch_primary(self, index: int):
        """Switch to a different primary upstream."""
        if 0 <= index < len(self._upstreams):
            self._primary_index = index

    def add_upstream(self, upstream: dict[str, Any]):
        """Add a new upstream."""
        self._upstreams.append(upstream)
        # Rebuild executor with new upstream list
        self._executor.shutdown()
        self._executor = ConcurrentRequestExecutor(self._upstreams, self._config)

    def remove_upstream(self, url: str):
        """Remove an upstream by URL."""
        self._upstreams = [u for u in self._upstreams if u.get("url") != url]
        # Rebuild executor
        self._executor.shutdown()
        self._executor = ConcurrentRequestExecutor(self._upstreams, self._config)

    def execute(
        self,
        path: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        """Execute a request using the multi-upstream manager."""
        return self._executor.execute_single(path, method, headers, body)

    def get_status(self) -> dict[str, Any]:
        """Get status of all upstreams."""
        return {
            "upstreams": [
                {
                    "url": u.get("url"),
                    "is_primary": i == self._primary_index,
                }
                for i, u in enumerate(self._upstreams)
            ],
            "health": self._executor.get_health_status(),
        }

    def shutdown(self):
        """Shutdown the manager."""
        self._executor.shutdown()


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def create_upstream_pool(
    upstreams: list[dict[str, Any]],
    config: dict | None = None,
) -> MultiUpstreamManager:
    """Create a multi-upstream manager from configuration."""
    parsed_config = _concurrency_config(config) if config else ConcurrencyConfig()
    return MultiUpstreamManager(upstreams, config=parsed_config)


def get_concurrency_stats(manager: MultiUpstreamManager) -> dict[str, Any]:
    """Get concurrency statistics from a manager."""
    status = manager.get_status()
    return {
        "upstream_count": len(status.get("upstreams", [])),
        "healthy_count": sum(
            1 for h in status.get("health", {}).get("upstreams", {}).values()
            if h.get("is_healthy")
        ),
        "queue_size": status.get("health", {}).get("queue_size", 0),
        "active_requests": status.get("health", {}).get("active_requests", 0),
    }
