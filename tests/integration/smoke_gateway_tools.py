#!/usr/bin/env python3
"""End-to-end smoke test for Gateway real local tools.

Assumes the gateway is already running, for example:
  ./scripts/mimo_gateway.sh

The smoke deliberately avoids the upstream model path. It verifies the gateway's
real tool runtime: project discovery, file writes/edits/reads, coding shell,
network fetch/search, parallel calls, and SQLite request logging.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class LocalNetworkHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/search"):
            body = (
                '<html><body><a class="result__a" href="https://example.test/gateway">Gateway Search Result</a>'
                '<a class="result__snippet">local search snippet</a></body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"gateway network smoke ok"
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        self.do_GET()


def post_json(base_url: str, key: str, path: str, payload: dict[str, Any], timeout: float = 15) -> dict[str, Any]:
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(base_url: str, path: str, timeout: float = 10) -> dict[str, Any]:
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_tool(base_url: str, key: str, tool: str, arguments: dict[str, Any], timeout: float = 20, workspace_root: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool": tool, "arguments": arguments}
    if workspace_root:
        payload["workspace_root"] = workspace_root
    result = post_json(base_url, key, "/v1/tools/call", payload, timeout=timeout)
    if not result.get("success", False):
        raise AssertionError(f"{tool} failed: {json.dumps(result, ensure_ascii=False)[:2000]}")
    return result


def assert_contains(name: str, text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"{name}: expected {needle!r} in {text[:1000]!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8885"))
    parser.add_argument("--key", default=os.environ.get("DOWNSTREAM_API_KEY", "local-gateway-key"))
    parser.add_argument("--sqlite", default=os.environ.get("GATEWAY_SQLITE_LOG_PATH", "gateway_log.sqlite3"))
    args = parser.parse_args()

    sqlite_path = pathlib.Path(args.sqlite)
    before_sqlite_mtime = sqlite_path.stat().st_mtime if sqlite_path.exists() else None
    legacy_paths = [pathlib.Path(".gateway_requests.jsonl"), pathlib.Path(".gateway_tool_failures.jsonl"), pathlib.Path(".gateway_stats.json")]
    legacy_mtimes = {str(p): p.stat().st_mtime if p.exists() else None for p in legacy_paths}

    local_http = ThreadingHTTPServer(("127.0.0.1", 0), LocalNetworkHandler)
    thread = threading.Thread(target=local_http.serve_forever, daemon=True)
    thread.start()
    net_base = f"http://127.0.0.1:{local_http.server_address[1]}"

    checks: list[str] = []
    try:
        health = get_json(args.base_url, "/healthz")
        if not health.get("ok"):
            raise AssertionError(f"healthz not ok: {health}")
        checks.append("healthz")

        tree = call_tool(args.base_url, args.key, "Tree", {"path": ".", "max_depth": 2, "max_entries": 200})
        assert_contains("Tree", tree["content"], "src/")
        checks.append("project_tree")

        globbed = call_tool(args.base_url, args.key, "Glob", {"pattern": "src/*.py"})
        assert_contains("Glob", globbed["content"], "src/gateway_app.py")
        checks.append("project_glob")

        symbols = call_tool(args.base_url, args.key, "PythonSymbols", {"file_path": "src/gateway_app.py"})
        assert_contains("PythonSymbols", symbols["content"], "GatewayHandler")
        checks.append("python_symbols")

        target = ".gateway_smoke/tool_smoke.py"
        call_tool(args.base_url, args.key, "Write", {"file_path": target, "content": "print('alpha')\n"})
        call_tool(args.base_url, args.key, "Edit", {"file_path": target, "old_string": "alpha", "new_string": "beta"})
        read = call_tool(args.base_url, args.key, "Read", {"file_path": target})
        assert_contains("Read after Edit", read["content"], "beta")
        checks.append("write_edit_read")

        bash = call_tool(args.base_url, args.key, "Bash", {"command": f"python3 {target}", "timeout": 10})
        assert_contains("Bash", bash["content"], "exit_code=0")
        assert_contains("Bash", bash["content"], "beta")
        checks.append("coding_bash")

        code = call_tool(args.base_url, args.key, "code_interpreter", {"code": "print(6*7)", "timeout": 10})
        assert_contains("code_interpreter", code["content"], "42")
        checks.append("code_interpreter")

        fetched = call_tool(args.base_url, args.key, "WebFetch", {"url": net_base + "/page"})
        assert_contains("WebFetch", fetched["content"], "gateway network smoke ok")
        posted = call_tool(args.base_url, args.key, "WebFetch", {"url": net_base + "/page", "method": "POST", "json": {"hello": "world"}})
        assert_contains("WebFetch POST", posted["content"], "status: 200")
        checks.append("web_fetch")

        searched = call_tool(args.base_url, args.key, "WebSearch", {"query": "gateway", "search_url": net_base + "/search"})
        assert_contains("WebSearch", searched["content"], "Gateway Search Result")
        checks.append("web_search")

        image_path = pathlib.Path(".gateway_smoke/red.png")
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"))
        image = call_tool(args.base_url, args.key, "AnalyzeImage", {"path": str(image_path), "histogram": True})
        assert_contains("AnalyzeImage", image["content"], '"width": 1')
        checks.append("vision_image")

        intent = call_tool(args.base_url, args.key, "IntentDetect", {"text": "分析 @src/gateway_app.py 并修改代码，然后运行测试和查询网络"})
        assert_contains("IntentDetect", intent["content"], "project_analysis")
        assert_contains("IntentDetect", intent["content"], "code_change")
        assert_contains("IntentDetect", intent["content"], "network")
        checks.append("intent_detect")

        parallel = call_tool(
            args.base_url,
            args.key,
            "multi_tool_use.parallel",
            {
                "tool_uses": [
                    {"recipient_name": "calculator", "parameters": {"expression": "20+22"}},
                    {"recipient_name": "Glob", "parameters": {"pattern": "src/*.py", "limit": 3}},
                ]
            },
        )
        assert_contains("parallel", parallel["content"], "42")
        checks.append("parallel_tools")

        arbitrary = pathlib.Path(".gateway_smoke_external_project").resolve()
        if arbitrary.exists():
            shutil.rmtree(arbitrary)
        arbitrary.mkdir(parents=True)
        try:
            call_tool(args.base_url, args.key, "Write", {"file_path": "app.py", "content": "def calc(a, b):\n    return a + b\n"}, workspace_root=str(arbitrary))
            call_tool(
                args.base_url,
                args.key,
                "Write",
                {"file_path": "test_app.py", "content": "import unittest\nfrom app import calc\n\nclass CalcTest(unittest.TestCase):\n    def test_calc(self):\n        self.assertEqual(calc(2, 3), 5)\n\nif __name__ == '__main__':\n    unittest.main()\n"},
                workspace_root=str(arbitrary),
            )
            project_tree = call_tool(args.base_url, args.key, "Tree", {"path": ".", "max_depth": 2}, workspace_root=str(arbitrary))
            assert_contains("arbitrary Tree", project_tree["content"], "app.py")
            project_symbols = call_tool(args.base_url, args.key, "PythonSymbols", {"file_path": "app.py"}, workspace_root=str(arbitrary))
            assert_contains("arbitrary PythonSymbols", project_symbols["content"], "calc")
            first_test = call_tool(args.base_url, args.key, "Bash", {"command": "python3 -m unittest -v", "timeout": 15}, workspace_root=str(arbitrary))
            assert_contains("arbitrary unittest", first_test["content"], "exit_code=0")
            call_tool(args.base_url, args.key, "Edit", {"file_path": "app.py", "old_string": "return a + b", "new_string": "return a * b"}, workspace_root=str(arbitrary))
            call_tool(args.base_url, args.key, "Edit", {"file_path": "test_app.py", "old_string": "5)", "new_string": "6)"}, workspace_root=str(arbitrary))
            second_test = call_tool(args.base_url, args.key, "Bash", {"command": "python3 -m unittest -v", "timeout": 15}, workspace_root=str(arbitrary))
            assert_contains("arbitrary modified unittest", second_test["content"], "exit_code=0")
        finally:
            shutil.rmtree(arbitrary, ignore_errors=True)
        checks.append("arbitrary_project_analyze_modify_run")

        call_tool(args.base_url, args.key, "DeletePath", {"path": ".gateway_smoke", "recursive": True})
        checks.append("cleanup_write_artifacts")

        # Give SQLite writes a moment to land, then confirm SQLite is active and legacy files were not appended.
        time.sleep(0.2)
        if not sqlite_path.exists():
            raise AssertionError(f"SQLite log missing: {sqlite_path}")
        after_sqlite_mtime = sqlite_path.stat().st_mtime
        if before_sqlite_mtime is not None and after_sqlite_mtime < before_sqlite_mtime:
            raise AssertionError("SQLite log timestamp moved backwards")
        for p in legacy_paths:
            current = p.stat().st_mtime if p.exists() else None
            if current != legacy_mtimes[str(p)]:
                raise AssertionError(f"legacy file log changed unexpectedly: {p}")
        checks.append("sqlite_only_logging")

        print(json.dumps({"ok": True, "checks": checks, "tool_count": health.get("builtin_tool_count"), "sqlite": str(sqlite_path)}, ensure_ascii=False, indent=2))
        return 0
    finally:
        local_http.shutdown()
        local_http.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise
