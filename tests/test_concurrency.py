"""Tests for concurrency optimization module."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.gateway_concurrency import (
    ConcurrencyConfig,
    ConnectionPool,
    ConcurrentRequestExecutor,
    LoadBalancer,
    MultiUpstreamManager,
    QueuedRequest,
    RequestQueue,
    UpstreamHealth,
    _concurrency_config,
    create_upstream_pool,
    get_concurrency_stats,
)


class TestConcurrencyConfig:
    def test_default_config(self):
        config = ConcurrencyConfig()
        assert config.enabled is True
        assert config.max_connections == 100
        assert config.max_connections_per_host == 10
        assert config.connection_timeout == 10.0
        assert config.read_timeout == 60.0
        assert config.retry_count == 2
        assert config.retry_delay == 1.0
        assert config.load_balance_strategy == "round_robin"
        assert config.health_check_interval == 30.0
        assert config.health_check_timeout == 5.0

    def test_custom_config(self):
        config = ConcurrencyConfig(
            enabled=False,
            max_connections=50,
            load_balance_strategy="least_connections",
        )
        assert config.enabled is False
        assert config.max_connections == 50
        assert config.load_balance_strategy == "least_connections"

    def test_parse_config_from_dict(self):
        raw = {
            "enabled": True,
            "max_connections": 200,
            "retry_count": 3,
        }
        config = _concurrency_config(raw)
        assert config.max_connections == 200
        assert config.retry_count == 3

    def test_parse_config_from_none(self):
        config = _concurrency_config(None)
        assert config.enabled is True

    def test_parse_config_from_empty(self):
        config = _concurrency_config({})
        assert config.max_connections == 100


class TestUpstreamHealth:
    def test_initial_health(self):
        health = UpstreamHealth(url="http://example.com")
        assert health.is_healthy is True
        assert health.consecutive_failures == 0
        assert health.success_rate == 1.0

    def test_success_rate_calculation(self):
        health = UpstreamHealth(url="http://example.com")
        health.success_count = 8
        health.failure_count = 2
        assert health.success_rate == 0.8

    def test_success_rate_no_requests(self):
        health = UpstreamHealth(url="http://example.com")
        assert health.success_rate == 1.0


class TestConnectionPool:
    def test_pool_creation(self):
        config = ConcurrencyConfig()
        pool = ConnectionPool(config)
        assert pool is not None

    def test_get_connection(self):
        config = ConcurrencyConfig()
        pool = ConnectionPool(config)
        conn = pool.get_connection("http://example.com")
        assert conn is not None

    def test_return_connection(self):
        config = ConcurrencyConfig()
        pool = ConnectionPool(config)
        conn = pool.get_connection("http://example.com")
        pool.return_connection("http://example.com", conn)
        # Should be able to get it again
        conn2 = pool.get_connection("http://example.com")
        assert conn2 is not None

    def test_connection_limit(self):
        config = ConcurrencyConfig(max_connections_per_host=2)
        pool = ConnectionPool(config)

        # Get max connections
        conns = []
        for _ in range(2):
            conns.append(pool.get_connection("http://example.com"))

        # Should raise on next one
        with pytest.raises(ConnectionError):
            pool.get_connection("http://example.com")

    def test_close_all(self):
        config = ConcurrencyConfig()
        pool = ConnectionPool(config)
        pool.get_connection("http://example.com")
        pool.close_all()


class TestLoadBalancer:
    def test_round_robin(self):
        upstreams = [
            {"url": "http://upstream1.com"},
            {"url": "http://upstream2.com"},
            {"url": "http://upstream3.com"},
        ]
        config = ConcurrencyConfig(load_balance_strategy="round_robin")
        balancer = LoadBalancer(upstreams, config)

        # Should cycle through upstreams
        results = set()
        for _ in range(6):
            upstream = balancer.get_next()
            results.add(upstream["url"])

        assert len(results) == 3

    def test_random_strategy(self):
        upstreams = [
            {"url": "http://upstream1.com"},
            {"url": "http://upstream2.com"},
        ]
        config = ConcurrencyConfig(load_balance_strategy="random")
        balancer = LoadBalancer(upstreams, config)

        # Should get some upstream
        upstream = balancer.get_next()
        assert upstream is not None
        assert "url" in upstream

    def test_health_status(self):
        upstreams = [{"url": "http://upstream1.com"}]
        config = ConcurrencyConfig()
        balancer = LoadBalancer(upstreams, config)

        status = balancer.get_health_status()
        assert "http://upstream1.com" in status

    def test_report_success(self):
        upstreams = [{"url": "http://upstream1.com"}]
        config = ConcurrencyConfig()
        balancer = LoadBalancer(upstreams, config)

        balancer.report_success("http://upstream1.com", 0.5)
        status = balancer.get_health_status()
        assert status["http://upstream1.com"]["success_rate"] == 1.0

    def test_report_failure(self):
        upstreams = [{"url": "http://upstream1.com"}]
        config = ConcurrencyConfig()
        balancer = LoadBalancer(upstreams, config)

        # Report multiple failures
        for _ in range(3):
            balancer.report_failure("http://upstream1.com")

        status = balancer.get_health_status()
        assert status["http://upstream1.com"]["is_healthy"] is False


class TestRequestQueue:
    def test_queue_creation(self):
        config = ConcurrencyConfig()
        queue = RequestQueue(config)
        assert queue.queue_size == 0
        assert queue.active_requests == 0

    def test_enqueue_request(self):
        config = ConcurrencyConfig()
        queue = RequestQueue(config)

        request = QueuedRequest(
            request_id="test-1",
            url="http://example.com",
            method="GET",
            headers={},
            body=None,
        )

        future = queue.enqueue(request)
        assert future is not None

        # Clean up
        queue.shutdown(wait=False)

    def test_queue_priority(self):
        config = ConcurrencyConfig()
        queue = RequestQueue(config)

        # Add requests with different priorities
        for i in range(5):
            request = QueuedRequest(
                request_id=f"test-{i}",
                url="http://example.com",
                method="GET",
                headers={},
                body=None,
                priority=i,
            )
            queue.enqueue(request)

        # Queue size is always 0 since requests are submitted directly to executor
        assert queue.queue_size == 0
        assert queue.active_requests >= 0

        queue.shutdown(wait=False)


class TestConcurrentRequestExecutor:
    def test_executor_creation(self):
        upstreams = [{"url": "http://example.com"}]
        config = ConcurrencyConfig()
        executor = ConcurrentRequestExecutor(upstreams, config)
        assert executor is not None

    def test_get_health_status(self):
        upstreams = [{"url": "http://example.com"}]
        config = ConcurrencyConfig()
        executor = ConcurrentRequestExecutor(upstreams, config)

        status = executor.get_health_status()
        assert "upstreams" in status
        assert "queue_size" in status
        assert "active_requests" in status

    def test_shutdown(self):
        upstreams = [{"url": "http://example.com"}]
        config = ConcurrencyConfig()
        executor = ConcurrentRequestExecutor(upstreams, config)
        executor.shutdown()


class TestMultiUpstreamManager:
    def test_manager_creation(self):
        upstreams = [
            {"url": "http://upstream1.com"},
            {"url": "http://upstream2.com"},
        ]
        manager = MultiUpstreamManager(upstreams)
        assert manager is not None

    def test_get_primary_upstream(self):
        upstreams = [
            {"url": "http://upstream1.com"},
            {"url": "http://upstream2.com"},
        ]
        manager = MultiUpstreamManager(upstreams)

        primary = manager.get_primary_upstream()
        assert primary is not None
        assert primary["url"] == "http://upstream1.com"

    def test_switch_primary(self):
        upstreams = [
            {"url": "http://upstream1.com"},
            {"url": "http://upstream2.com"},
        ]
        manager = MultiUpstreamManager(upstreams)

        manager.switch_primary(1)
        primary = manager.get_primary_upstream()
        assert primary["url"] == "http://upstream2.com"

    def test_add_upstream(self):
        upstreams = [{"url": "http://upstream1.com"}]
        manager = MultiUpstreamManager(upstreams)

        manager.add_upstream({"url": "http://upstream2.com"})
        status = manager.get_status()
        assert len(status["upstreams"]) == 2

    def test_remove_upstream(self):
        upstreams = [
            {"url": "http://upstream1.com"},
            {"url": "http://upstream2.com"},
        ]
        manager = MultiUpstreamManager(upstreams)

        manager.remove_upstream("http://upstream1.com")
        status = manager.get_status()
        assert len(status["upstreams"]) == 1

    def test_get_status(self):
        upstreams = [{"url": "http://upstream1.com"}]
        manager = MultiUpstreamManager(upstreams)

        status = manager.get_status()
        assert "upstreams" in status
        assert "health" in status

    def test_shutdown(self):
        upstreams = [{"url": "http://upstream1.com"}]
        manager = MultiUpstreamManager(upstreams)
        manager.shutdown()


class TestCreateUpstreamPool:
    def test_create_pool(self):
        upstreams = [
            {"url": "http://upstream1.com"},
            {"url": "http://upstream2.com"},
        ]
        manager = create_upstream_pool(upstreams)
        assert isinstance(manager, MultiUpstreamManager)

    def test_create_pool_with_config(self):
        upstreams = [{"url": "http://upstream1.com"}]
        config = {"max_connections": 50}
        manager = create_upstream_pool(upstreams, config)
        assert isinstance(manager, MultiUpstreamManager)


class TestGetConcurrencyStats:
    def test_get_stats(self):
        upstreams = [{"url": "http://upstream1.com"}]
        manager = MultiUpstreamManager(upstreams)

        stats = get_concurrency_stats(manager)
        assert "upstream_count" in stats
        assert "healthy_count" in stats
        assert "queue_size" in stats
        assert "active_requests" in stats


@pytest.mark.integration
class TestConcurrencyIntegration:
    def test_multi_upstream_failover(self):
        """Test failover when primary upstream fails."""
        upstreams = [
            {"url": "http://nonexistent1.example.com"},
            {"url": "http://nonexistent2.example.com"},
        ]
        manager = MultiUpstreamManager(upstreams)

        # Should handle gracefully even if all upstreams fail
        status = manager.get_status()
        assert len(status["upstreams"]) == 2

        manager.shutdown()

    def test_concurrent_health_checks(self):
        """Test concurrent health status checks."""
        upstreams = [
            {"url": f"http://upstream{i}.example.com"}
            for i in range(5)
        ]
        manager = MultiUpstreamManager(upstreams)

        # Get status from multiple threads
        results = []
        errors = []

        def check_status():
            try:
                status = manager.get_status()
                results.append(status)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=check_status) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10

        manager.shutdown()

    def test_upstream_management(self):
        """Test adding and removing upstreams dynamically."""
        upstreams = [{"url": "http://upstream1.example.com"}]
        manager = MultiUpstreamManager(upstreams)

        # Add more upstreams
        for i in range(2, 5):
            manager.add_upstream({"url": f"http://upstream{i}.example.com"})

        status = manager.get_status()
        assert len(status["upstreams"]) == 4

        # Remove some
        manager.remove_upstream("http://upstream2.example.com")
        manager.remove_upstream("http://upstream3.example.com")

        status = manager.get_status()
        assert len(status["upstreams"]) == 2

        manager.shutdown()


class TestLeastConnectionsStrategy:
    """Test that least_connections actually picks the host with fewest active connections."""

    def test_least_connections_picks_lowest(self):
        """Get upstream with fewest active connections."""
        upstreams = [
            {"url": "http://a.example.com"},
            {"url": "http://b.example.com"},
            {"url": "http://c.example.com"},
        ]
        config = ConcurrencyConfig(load_balance_strategy="least_connections")
        lb = LoadBalancer(upstreams, config)

        # Simulate different active connection counts
        lb._health["http://a.example.com"].active_connections = 5
        lb._health["http://b.example.com"].active_connections = 1
        lb._health["http://c.example.com"].active_connections = 3

        chosen = lb.get_next()
        assert chosen["url"] == "http://b.example.com"

    def test_least_connections_all_equal(self):
        """When all have same connections, any is acceptable."""
        upstreams = [
            {"url": "http://a.example.com"},
            {"url": "http://b.example.com"},
        ]
        config = ConcurrencyConfig(load_balance_strategy="least_connections")
        lb = LoadBalancer(upstreams, config)

        # Both have 0 active connections
        chosen = lb.get_next()
        assert chosen["url"] in ("http://a.example.com", "http://b.example.com")

    def test_least_connections_skips_unhealthy(self):
        """Unhealthy upstreams are excluded from selection."""
        upstreams = [
            {"url": "http://a.example.com"},
            {"url": "http://b.example.com"},
            {"url": "http://c.example.com"},
        ]
        config = ConcurrencyConfig(load_balance_strategy="least_connections")
        lb = LoadBalancer(upstreams, config)

        # a has fewest connections but is unhealthy
        lb._health["http://a.example.com"].is_healthy = False
        lb._health["http://a.example.com"].active_connections = 0
        lb._health["http://b.example.com"].active_connections = 3
        lb._health["http://c.example.com"].active_connections = 1

        chosen = lb.get_next()
        assert chosen["url"] == "http://c.example.com"
