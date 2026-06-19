"""Gateway package bootstrap.

Loopback HTTP is part of normal gateway operation: tests and local runtime
smokes start mock upstreams, admin endpoints, and Web2API servers on
``127.0.0.1``.  On macOS, ``urllib.request`` can inherit system proxy settings
even when the shell environment is clean, which sends loopback requests through
a proxy and causes flaky ``RemoteDisconnected`` errors.

Keep localhost traffic local by default while preserving any existing
``NO_PROXY`` / ``no_proxy`` entries.
"""
from __future__ import annotations

import os


def _ensure_loopback_proxy_bypass() -> None:
    loopback = ("127.0.0.1", "localhost", "::1")
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    present = {part.lower() for part in parts}
    for host in loopback:
        if host.lower() not in present:
            parts.append(host)
    value = ",".join(parts)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


_ensure_loopback_proxy_bypass()
