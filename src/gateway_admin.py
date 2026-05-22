#!/usr/bin/env python3
"""Admin UI rendering for the gateway.

Handles rendering of the admin web interface and client configuration snippets.
"""
from __future__ import annotations

import html
import json
import os
from typing import Any

Json = dict[str, Any]


def _client_snippet_context() -> Json:
    from .gateway_config import load_config
    cfg = load_config()
    gateway_cfg = cfg.get("gateway", {})
    return {
        "public_base_url": gateway_cfg.get("public_base_url", "http://127.0.0.1:8885"),
        "client_snippet_api_key": gateway_cfg.get("client_snippet_api_key", ""),
        "downstream_model_alias": gateway_cfg.get("downstream_model_alias", ""),
        "review_model_alias": gateway_cfg.get("review_model_alias", ""),
        "codex_reasoning_effort": gateway_cfg.get("codex_reasoning_effort", "xhigh"),
        "client_context_window": gateway_cfg.get("client_context_window", 1000000),
        "client_auto_compact_token_limit": gateway_cfg.get("client_auto_compact_token_limit", 900000),
        "client_output_token_limit": gateway_cfg.get("client_output_token_limit", 128000),
    }


def _toml_string(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _client_config_snippets() -> Json:
    ctx = _client_snippet_context()
    base_url = ctx["public_base_url"].rstrip("/")
    api_key = ctx["client_snippet_api_key"]
    model_alias = ctx["downstream_model_alias"]
    openai_base = base_url + "/v1"
    codex_config_toml = (
        'model_provider = "Gateway"\n'
        f'model = "{_toml_string(model_alias)}"\n'
        'wire_api = "responses"\n'
        f'base_url = "{_toml_string(openai_base)}"\n'
        f'reasoning_effort = "{_toml_string(ctx["codex_reasoning_effort"])}"\n'
    )
    codex_auth_json = json.dumps({"OPENAI_API_KEY": api_key}, ensure_ascii=False, indent=2)
    opencode_json = json.dumps({"provider": {"gateway": {"baseURL": openai_base, "apiKey": api_key}}, "model": model_alias}, ensure_ascii=False, indent=2)
    claude_bash_profile_function = (
        "gateway-claude() {\n"
        f'  ANTHROPIC_BASE_URL="{base_url}" ANTHROPIC_AUTH_TOKEN="{api_key}" claude "$@"\n'
        "}\n"
    )
    vscode_claude_settings_json = json.dumps({"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": api_key}, ensure_ascii=False, indent=2)
    return {
        "openai_base_url": openai_base,
        "anthropic_base_url": base_url,
        "openai_api_key": api_key,
        "anthropic_api_key": api_key,
        "model_alias": model_alias,
        "codex_config_toml": codex_config_toml,
        "codex_auth_json": codex_auth_json,
        "opencode_json": opencode_json,
        "claude_bash_profile_function": claude_bash_profile_function,
        "vscode_claude_settings_json": vscode_claude_settings_json,
        "codex_config": {
            "model": model_alias,
            "base_url": openai_base,
            "api_key": api_key,
            "reasoning_effort": ctx["codex_reasoning_effort"],
        },
        "claude_code_env": {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": api_key,
        },
        "opencode_env": {
            "OPENAI_BASE_URL": openai_base,
            "OPENAI_API_KEY": api_key,
        },
    }


def _render_client_config_ui() -> str:
    ctx = _client_snippet_context()
    base_url = html.escape(ctx['public_base_url'])
    api_key = html.escape(ctx['client_snippet_api_key'])

    return """<!DOCTYPE html>
<html>
<head>
<title>Gateway Client Configuration</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }
h1 { color: #333; }
.config-section { background: #f5f5f5; padding: 15px; border-radius: 8px; margin: 15px 0; }
pre { background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 6px; overflow-x: auto; }
code { font-family: 'SF Mono', Monaco, monospace; }
</style>
</head>
<body>
<h1>Gateway Client Configuration</h1>

<div class="config-section">
<h3>Claude Code</h3>
<pre><code>export ANTHROPIC_BASE_URL="%s"
export ANTHROPIC_API_KEY="%s"
claude</code></pre>
</div>

<div class="config-section">
<h3>OpenAI SDK / Codex / OpenCode</h3>
<pre><code>export OPENAI_BASE_URL="%s/v1"
export OPENAI_API_KEY="%s"</code></pre>
</div>

<div class="config-section">
<h3>Python SDK</h3>
<pre><code>from openai import OpenAI
client = OpenAI(
    base_url="%s/v1",
    api_key="%s"
)</code></pre>
</div>

<p><a href="/ui">Back to Admin UI</a></p>
</body>
</html>""" % (base_url, api_key, base_url, api_key, base_url, api_key)


def _render_admin_ui() -> str:
    from .gateway_config import load_config, _redacted_config
    from .gateway_logging import _stats_snapshot, _tail_requests, _tail_failures, _tool_catalog_snapshot
    from .gateway_context import _sqlite_tail_memories
    from .gateway_mcp import _enabled_mcp_servers, _mcp_list_server_tools, _mcp_public_name
    from .gateway_http_actions import _enabled_http_actions

    cfg = load_config()
    redacted = _redacted_config(cfg)
    stats = _stats_snapshot()
    requests = _tail_requests(20)
    failures = _tail_failures(20)
    memories = _sqlite_tail_memories(20)
    tools = _tool_catalog_snapshot()
    mcp_servers = _enabled_mcp_servers()

    mcp_tools = []
    for server in mcp_servers:
        server_name = str(server.get("name") or "")
        try:
            for tool in _mcp_list_server_tools(server):
                mcp_tools.append({
                    "server": server_name,
                    "name": tool.get("name"),
                    "gateway_name": _mcp_public_name(server_name, str(tool.get("name"))),
                    "description": tool.get("description"),
                })
        except Exception as exc:
            mcp_tools.append({"server": server_name, "error": str(exc)})

    http_actions = _enabled_http_actions()
    upstream_profiles = cfg.get("upstream_profiles") or []
    active_upstream_id = cfg.get("active_upstream_id", "")
    downstream_keys = cfg.get("downstream_keys") or []

    # Build upstream profiles rows
    profile_rows = []
    for p in upstream_profiles:
        is_active = p.get("id") == active_upstream_id
        style = ' style="background:#e8f5e9;"' if is_active else ''
        row = '<tr%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>' % (
            style,
            html.escape(str(p.get("name", ""))),
            html.escape(str(p.get("base_url", ""))),
            html.escape(str(p.get("model", ""))),
            html.escape(str(p.get("protocol", ""))),
        )
        row += '<form method="POST" action="/admin/upstream-profile"><input type="hidden" name="action" value="activate"><input type="hidden" name="id" value="%s"><button type="submit">Activate</button></form>' % html.escape(str(p.get("id", "")))
        row += '<form method="POST" action="/admin/upstream-profile"><input type="hidden" name="action" value="delete"><input type="hidden" name="id" value="%s"><button type="submit" class="danger">Delete</button></form>' % html.escape(str(p.get("id", "")))
        row += '</td></tr>'
        profile_rows.append(row)

    # Build downstream keys rows
    key_rows = []
    for k in downstream_keys:
        row = '<tr><td>%s</td><td>%s</td><td>%s</td><td>' % (
            html.escape(str(k.get("name", ""))),
            html.escape(str(k.get("prefix", ""))),
            "Yes" if k.get("enabled", True) else "No",
        )
        row += '<form method="POST" action="/admin/downstream-key"><input type="hidden" name="action" value="delete"><input type="hidden" name="name" value="%s"><button type="submit" class="danger">Delete</button></form>' % html.escape(str(k.get("name", "")))
        row += '</td></tr>'
        key_rows.append(row)

    # Build MCP servers rows
    mcp_rows = []
    for s in mcp_servers:
        enabled_badge = '<span class="badge badge-ok">Enabled</span>' if s.get("enabled", True) else '<span class="badge badge-error">Disabled</span>'
        tool_count = len([t for t in mcp_tools if t.get("server") == s.get("name")])
        row = '<tr><td>%s</td><td>%s</td><td>%d</td><td>' % (
            html.escape(str(s.get("name", ""))),
            enabled_badge,
            tool_count,
        )
        row += '<form method="POST" action="/admin/mcp"><input type="hidden" name="action" value="delete"><input type="hidden" name="name" value="%s"><button type="submit" class="danger">Delete</button></form>' % html.escape(str(s.get("name", "")))
        row += '</td></tr>'
        mcp_rows.append(row)

    # Build builtin tools rows
    tool_rows = []
    tool_items = tools.get("tools", [])
    if isinstance(tool_items, dict):
        tool_items = list(tool_items.values())
    tool_name_count = len(tool_items)
    unique_tool_count = len(
        {
            str(t.get("canonical_name") or t.get("name") or "")
            for t in tool_items
            if isinstance(t, dict) and (t.get("canonical_name") or t.get("name"))
        }
    )
    for t in tool_items:
        row = '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            html.escape(str(t.get("name", ""))),
            html.escape(str(t.get("canonical_name") or t.get("name", ""))),
            html.escape(str(t.get("risk", ""))),
            html.escape(str(t.get("description", ""))[:100]),
        )
        tool_rows.append(row)

    # Build recent requests rows
    request_rows = []
    for r in requests[:10]:
        row = '<tr><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            html.escape(str(r.get("ts", ""))[:19]),
            html.escape(str(r.get("path", ""))),
            str(r.get("status", "")),
        )
        request_rows.append(row)

    # Build recent failures rows
    failure_rows = []
    for f in failures[:10]:
        row = '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            html.escape(str(f.get("ts", ""))[:19]),
            html.escape(str(f.get("tool_name", ""))),
            html.escape(str(f.get("failure_type", ""))),
            html.escape(str(f.get("content", ""))[:80]),
        )
        failure_rows.append(row)

    # Build memories rows
    memory_rows = []
    for m in memories[:10]:
        row = '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            html.escape(str(m.get("ts", ""))[:19]),
            html.escape(str(m.get("session_key", ""))[:12]),
            html.escape(str(m.get("kind", ""))),
            html.escape(str(m.get("summary", ""))[:100]),
        )
        memory_rows.append(row)

    return """<!DOCTYPE html>
<html>
<head>
<title>Gateway Admin</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 1200px; margin: 20px auto; padding: 0 20px; }
h1 { color: #333; }
h2 { color: #555; border-bottom: 2px solid #eee; padding-bottom: 8px; }
.section { background: #f9f9f9; padding: 15px; border-radius: 8px; margin: 15px 0; }
pre { background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; }
table { width: 100%%; border-collapse: collapse; }
th, td { padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }
th { background: #f0f0f0; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; }
.badge-ok { background: #d4edda; color: #155724; }
.badge-error { background: #f8d7da; color: #721c24; }
form { display: inline; }
input, select { padding: 6px; margin: 2px; }
button { background: #0066cc; color: white; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; }
button:hover { background: #0052a3; }
button.danger { background: #dc3545; }
button.danger:hover { background: #c82333; }
</style>
</head>
<body>
<h1>Gateway Admin</h1>
<p><a href="/client-config">Client Configuration</a> | <a href="/admin/config.json">Config JSON</a> | <a href="/admin/stats.json">Stats JSON</a></p>

<div class="section">
<h2>添加/编辑上游 API 详情</h2>
<table>
<tr><th>Name</th><th>Base URL</th><th>Model</th><th>Protocol</th><th>Actions</th></tr>
%s
</table>
<h3>Add Profile</h3>
<form method="POST" action="/admin/upstream-profile">
<input type="hidden" name="action" value="save">
<input type="text" name="name" placeholder="Name" required>
<input type="text" name="base_url" placeholder="Base URL" required>
<input type="text" name="api_key" placeholder="API Key">
<input type="text" name="model" placeholder="Model">
<select name="protocol"><option value="openai_chat">OpenAI Chat</option><option value="anthropic_messages">Anthropic Messages</option><option value="openai_responses">OpenAI Responses</option></select>
<button type="submit">Add Profile</button>
</form>
</div>

<div class="section">
<h2>Downstream Keys</h2>
<table>
<tr><th>Name</th><th>Prefix</th><th>Enabled</th><th>Actions</th></tr>
%s
</table>
<h3>Add Key</h3>
<form method="POST" action="/admin/downstream-key">
<input type="hidden" name="action" value="add">
<input type="text" name="name" placeholder="Name" required>
<input type="text" name="key" placeholder="API Key" required>
<button type="submit">Add Key</button>
</form>
</div>

<div class="section">
<h2>MCP Servers</h2>
<table>
<tr><th>Name</th><th>Status</th><th>Tools</th><th>Actions</th></tr>
%s
</table>
<h3>Add Server</h3>
<form method="POST" action="/admin/mcp">
<input type="hidden" name="action" value="add">
<input type="text" name="name" placeholder="Name" required>
<input type="text" name="command" placeholder="Command (e.g., npx -y @modelcontextprotocol/server-filesystem /path)" required style="width:50%%">
<button type="submit">Add Server</button>
</form>
<form method="POST" action="/admin/mcp-reload"><button type="submit">Reload All</button></form>
</div>

<div class="section">
<h2>Builtin Tools (%d unique / %d exposed names)</h2>
<table>
<tr><th>Name</th><th>Canonical</th><th>Risk</th><th>Description</th></tr>
%s
</table>
</div>

<div class="section">
<h2>Statistics</h2>
<pre><code>%s</code></pre>
</div>

<div class="section">
<h2>Recent Requests</h2>
<table>
<tr><th>Time</th><th>Path</th><th>Status</th></tr>
%s
</table>
</div>

<div class="section">
<h2>Recent Failures</h2>
<table>
<tr><th>Time</th><th>Tool</th><th>Type</th><th>Content</th></tr>
%s
</table>
</div>

<div class="section">
<h2>Conversation Memories</h2>
<table>
<tr><th>Time</th><th>Session</th><th>Kind</th><th>Summary</th></tr>
%s
</table>
</div>

</body>
</html>""" % (
        "\n".join(profile_rows),
        "\n".join(key_rows),
        "\n".join(mcp_rows),
        unique_tool_count,
        tool_name_count,
        "\n".join(tool_rows),
        html.escape(json.dumps(stats, indent=2, ensure_ascii=False)),
        "\n".join(request_rows),
        "\n".join(failure_rows),
        "\n".join(memory_rows),
    )
