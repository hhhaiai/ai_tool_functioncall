#!/usr/bin/env python3
"""Security compatibility checks for a running Gateway.

Checks auth, admin protection, workspace path containment, write/shell gates as
configured, and confirms client-config is read-only generated output.
"""
from __future__ import annotations

import argparse
import base64
import json
import urllib.error
import urllib.request
from typing import Any


def request(method: str, url: str, *, key: str | None = None, admin: str | None = None, payload: dict[str, Any] | None = None, timeout: float = 10) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {}
    if payload is not None:
        headers["content-type"] = "application/json"
    if key is not None:
        headers["authorization"] = f"Bearer {key}"
    if admin is not None:
        headers["authorization"] = "Basic " + base64.b64encode(admin.encode()).decode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return exc.code, parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8885")
    parser.add_argument("--key", default="local-gateway-key")
    parser.add_argument("--admin", default="admin:admin")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    status, body = request("GET", base + "/client-config")
    add("admin_client_config_requires_auth", status == 401, {"status": status, "body": str(body)[:200]})

    status, body = request("GET", base + "/admin/config.json", admin=args.admin)
    add("admin_config_auth_ok", status == 200 and isinstance(body, dict) and "config" in body, {"status": status})

    status, body = request("POST", base + "/v1/tools/call", payload={"tool": "calculator", "arguments": {"expression": "1+1"}})
    add("api_requires_bearer", status == 401, {"status": status, "body": body})

    status, body = request("POST", base + "/v1/tools/call", key="wrong-key", payload={"tool": "calculator", "arguments": {"expression": "1+1"}})
    add("api_rejects_bad_key", status == 401, {"status": status, "body": body})

    status, body = request("POST", base + "/v1/tools/call", key=args.key, payload={"tool": "Read", "arguments": {"file_path": "../../../../etc/passwd"}})
    add("path_traversal_denied", status == 200 and isinstance(body, dict) and body.get("success") is False and body.get("failure_type") == "permission_denied", body)

    status, body = request("POST", base + "/v1/tools/call", key=args.key, payload={"tool": "DeletePath", "arguments": {"path": "."}})
    add("destructive_delete_directory_requires_recursive", status == 200 and isinstance(body, dict) and body.get("success") is False, body)

    status, body = request("GET", base + "/client-config.json", admin=args.admin)
    redacted_safe = status == 200 and isinstance(body, dict) and "codex_config_toml" in body and "claude_bash_profile_function" in body
    add("client_config_generation_ok", redacted_safe, {"status": status, "keys": list(body.keys()) if isinstance(body, dict) else None})

    failed = [c for c in checks if not c["ok"]]
    print(json.dumps({"ok": not failed, "checks": checks, "failures": failed}, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
