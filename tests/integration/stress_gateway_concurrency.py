#!/usr/bin/env python3
"""Concurrent smoke/stress test for the local Gateway.

It exercises direct tools, token counting, and chat/messages paths concurrently.
The chat/messages prompts are small to keep upstream cost low; direct tools do
not require upstream.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.request
from typing import Any


def post_json(base_url: str, key: str, path: str, payload: dict[str, Any], timeout: float) -> tuple[float, int, dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        elapsed = time.perf_counter() - start
        return elapsed, resp.status, json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8885")
    parser.add_argument("--key", default="local-gateway-key")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--direct-tool-requests", type=int, default=80)
    parser.add_argument("--model-requests", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    jobs: list[tuple[str, str, dict[str, Any]]] = []
    for i in range(args.direct_tool_requests):
        if i % 4 == 0:
            jobs.append(("tool_calc", "/v1/tools/call", {"tool": "calculator", "arguments": {"expression": f"{i}+{i}"}}))
        elif i % 4 == 1:
            jobs.append(("tool_glob", "/v1/tools/call", {"tool": "Glob", "arguments": {"pattern": "src/*.py", "limit": 10}}))
        elif i % 4 == 2:
            jobs.append(("tool_lsp", "/v1/tools/call", {"tool": "LSP", "arguments": {"action": "document_symbols", "file_path": "src/gateway_app.py"}}))
        else:
            jobs.append(("token_count", "/v1/messages/count_tokens", {"model": "mimo-v2.5-pro", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 10}))
    for i in range(args.model_requests):
        jobs.append(("chat", "/v1/chat/completions", {"model": "mimo-v2.5-pro", "messages": [{"role": "user", "content": f"Reply ok {i} in one word."}], "max_tokens": 20}))
        jobs.append(("messages", "/v1/messages", {"model": "mimo-v2.5-pro", "messages": [{"role": "user", "content": f"Reply ok {i} in one word."}], "max_tokens": 20}))

    results: list[dict[str, Any]] = []

    def run(job: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
        label, path, payload = job
        try:
            elapsed, status, body = post_json(args.base_url, args.key, path, payload, args.timeout)
            ok = status == 200 and not (isinstance(body, dict) and body.get("error"))
            return {"label": label, "path": path, "ok": ok, "status": status, "elapsed": elapsed, "error": None}
        except Exception as exc:
            return {"label": label, "path": path, "ok": False, "status": None, "elapsed": None, "error": f"{type(exc).__name__}: {exc}"}

    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        for result in executor.map(run, jobs):
            results.append(result)
    total_elapsed = time.perf_counter() - start

    failures = [r for r in results if not r["ok"]]
    latencies = [float(r["elapsed"]) for r in results if r.get("elapsed") is not None]
    by_label: dict[str, dict[str, Any]] = {}
    for r in results:
        item = by_label.setdefault(r["label"], {"total": 0, "ok": 0})
        item["total"] += 1
        item["ok"] += 1 if r["ok"] else 0
    summary = {
        "ok": not failures,
        "total": len(results),
        "failures": failures[:10],
        "workers": args.workers,
        "total_elapsed_seconds": round(total_elapsed, 3),
        "latency_seconds": {
            "min": round(min(latencies), 4) if latencies else None,
            "p50": round(statistics.median(latencies), 4) if latencies else None,
            "max": round(max(latencies), 4) if latencies else None,
        },
        "by_label": by_label,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
