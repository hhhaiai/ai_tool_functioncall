import pathlib
import re
import os
import tempfile
import subprocess
import time
import urllib.request
import signal
from unittest.mock import patch

from src import gateway_config, gateway_encryption, gateway_stats
from src.gateway_tool_runtime import _create_anonymous_workspace


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_production_gateway_port_is_not_published_on_all_interfaces():
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    gateway_block = compose.split("  nginx:", 1)[0]
    published = re.findall(r'^\s+-\s+"([^"]*:8885)"\s*$', gateway_block, re.MULTILINE)

    assert all(value.startswith("127.0.0.1:") for value in published)
    assert "GATEWAY_EXECUTE_USER_SIDE_TOOLS=${GATEWAY_EXECUTE_USER_SIDE_TOOLS:-0}" in gateway_block
    assert "GATEWAY_DOWNSTREAM_KEY=${GATEWAY_DOWNSTREAM_KEY:?set GATEWAY_DOWNSTREAM_KEY}" in gateway_block
    assert "GATEWAY_ADMIN_PASSWORD=${GATEWAY_ADMIN_PASSWORD:?set GATEWAY_ADMIN_PASSWORD}" in gateway_block
    assert re.search(r"networks:\s*\n\s+default:\s*\n(?:\s+#.*\n)*\s+enable_ipv6: false", compose)


def test_development_compose_is_loopback_only_and_exposure_is_explicit():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    prod = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert '"127.0.0.1:${GATEWAY_PORT:-8885}:8885"' in compose
    assert "GATEWAY_PUBLIC_EXPOSURE=private" in compose
    assert "GATEWAY_PUBLIC_EXPOSURE=external" in prod
    assert "GATEWAY_CORS_ENABLED=${GATEWAY_CORS_ENABLED:-0}" in compose
    assert "GATEWAY_CORS_ENABLED=${GATEWAY_CORS_ENABLED:-0}" in prod


def test_nginx_anthropic_route_preserves_streaming_tool_events():
    nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
    match = re.search(r"location /anthropic/ \{(?P<body>.*?)\n\s*\}", nginx, re.DOTALL)

    assert match is not None
    body = match.group("body")
    assert "proxy_pass http://gateway;" in body
    assert "proxy_http_version 1.1;" in body
    assert "proxy_buffering off;" in body
    assert "proxy_cache off;" in body
    assert "proxy_read_timeout 86400s;" in body


def test_production_nginx_enforces_tls_and_container_is_hardened():
    nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "return 308 https://$host$request_uri;" in nginx
    assert "listen 443 ssl;" in nginx
    assert "ssl_certificate /etc/nginx/ssl/cert.pem;" in nginx
    assert "ssl_certificate_key /etc/nginx/ssl/key.pem;" in nginx
    assert "Strict-Transport-Security" in nginx
    assert "client_max_body_size 64M;" in nginx
    assert "USER gateway:gateway" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    gateway_block = compose.split("  nginx:", 1)[0]
    assert "read_only: true" in gateway_block
    assert "cap_drop:" in gateway_block and "- ALL" in gateway_block
    assert "no-new-privileges:true" in gateway_block
    assert "pids: 256" in gateway_block
    assert "/readyz" in gateway_block


def test_gateway_liveness_readiness_and_sigterm_shutdown():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy()
        env.update({
            "GATEWAY_CONFIG_PATH": str(pathlib.Path(td) / "config.json"),
            "GATEWAY_RUNTIME_DIR": td,
            "GATEWAY_ADMIN_PASSWORD": "deployment-test-only",
        })
        proc = subprocess.Popen(
            ["python3", "-m", "src.toolcall_gateway", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10
            ready = None
            while time.time() < deadline and proc.poll() is None:
                try:
                    ready = urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=0.5)
                    break
                except Exception:
                    time.sleep(0.05)
            assert ready is not None and ready.status == 200
            assert urllib.request.urlopen(f"http://127.0.0.1:{port}/livez", timeout=1).status == 200
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_external_listener_rejects_default_credentials_before_binding():
    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy()
        env.update({
            "GATEWAY_CONFIG_PATH": str(pathlib.Path(td) / "config.json"),
            "GATEWAY_RUNTIME_DIR": td,
            "GATEWAY_PUBLIC_EXPOSURE": "external",
        })
        for name in (
            "GATEWAY_ADMIN_PASSWORD",
            "GATEWAY_ADMIN_PASSWORD_HASH",
            "GATEWAY_DOWNSTREAM_KEY",
            "DOWNSTREAM_API_KEY",
        ):
            env.pop(name, None)
        completed = subprocess.run(
            ["python3", "-m", "src.toolcall_gateway", "--host", "0.0.0.0", "--port", "0"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    assert completed.returncode != 0
    assert "refuses the default Admin password" in (completed.stdout + completed.stderr)


def test_external_listener_starts_with_runtime_credentials():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy()
        env.update({
            "GATEWAY_CONFIG_PATH": str(pathlib.Path(td) / "config.json"),
            "GATEWAY_RUNTIME_DIR": td,
            "GATEWAY_PUBLIC_EXPOSURE": "external",
            "GATEWAY_ADMIN_PASSWORD": "external-runtime-admin",
            "GATEWAY_DOWNSTREAM_KEY": "external-runtime-client",
        })
        proc = subprocess.Popen(
            ["python3", "-m", "src.toolcall_gateway", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10
            ready = None
            while time.time() < deadline and proc.poll() is None:
                try:
                    ready = urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=0.5)
                    break
                except Exception:
                    time.sleep(0.05)
            assert ready is not None and ready.status == 200
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0
            output = (proc.stdout.read() if proc.stdout else "") + (proc.stderr.read() if proc.stderr else "")
            assert "public exposure contract: external" in output
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_tool_output_is_bounded_and_expired_exec_sessions_are_reaped():
    from src import gateway_builtin_tools as tools

    with patch.dict(os.environ, {"GATEWAY_TOOL_OUTPUT_MAX_CHARS": "1024"}, clear=False):
        bounded = tools._bounded_tool_output("x" * 5000)
    assert len(bounded) < 1200
    assert "truncated" in bounded

    proc = subprocess.Popen(["sleep", "60"])
    key = "test-expired-session"
    try:
        with tools.EXEC_SESSIONS_LOCK:
            tools.EXEC_SESSIONS[key] = proc
            tools.EXEC_SESSION_LAST_USED[key] = 0
        with patch.dict(os.environ, {"GATEWAY_EXEC_SESSION_TTL_SECONDS": "1"}, clear=False):
            assert tools._reap_expired_exec_sessions(now=10) >= 1
        assert proc.wait(timeout=5) is not None
        with tools.EXEC_SESSIONS_LOCK:
            assert key not in tools.EXEC_SESSIONS
    finally:
        if proc.poll() is None:
            proc.kill()


def test_server_runtime_state_uses_persistent_runtime_directory():
    with tempfile.TemporaryDirectory() as td, patch.dict(
        os.environ, {"GATEWAY_RUNTIME_DIR": td}, clear=False
    ):
        runtime = pathlib.Path(td)
        config = gateway_config._default_config()
        anonymous = _create_anonymous_workspace(
            {"metadata": {"tenant": "server-user", "session_id": "server-session"}}
        )

        assert config["persistence"]["db_path"] == str(runtime / "gateway.db")
        assert gateway_encryption._get_key_path() == runtime / "encryption.key"
        assert gateway_stats._stats_db_path() == runtime / "stats.db"
        assert anonymous.is_relative_to((runtime / "anonymous_spaces").resolve())


def test_container_installs_encryption_and_persists_runtime_state():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(r"^cryptography(?:[<>=].*)?$", requirements, re.MULTILINE)
    assert "GATEWAY_RUNTIME_DIR=/app/data/runtime" in dockerfile
