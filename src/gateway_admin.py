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
    upstream_cfg = cfg.get("upstream", {}) if isinstance(cfg.get("upstream"), dict) else {}
    upstream_model = str(upstream_cfg.get("model") or "")
    return {
        "public_base_url": gateway_cfg.get("public_base_url", "http://127.0.0.1:8885"),
        "client_snippet_api_key": gateway_cfg.get("client_snippet_api_key", ""),
        "downstream_model_alias": gateway_cfg.get("downstream_model_alias") or upstream_model,
        "review_model_alias": gateway_cfg.get("review_model_alias") or gateway_cfg.get("downstream_model_alias") or upstream_model,
        "codex_reasoning_effort": gateway_cfg.get("codex_reasoning_effort", "xhigh"),
        "client_context_window": gateway_cfg.get("client_context_window", 1048576),
        "client_auto_compact_token_limit": gateway_cfg.get("client_auto_compact_token_limit", 943718),
        "client_output_token_limit": gateway_cfg.get("client_output_token_limit", 131072),
    }


def _toml_string(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _client_config_snippets() -> Json:
    ctx = _client_snippet_context()
    base_url = ctx["public_base_url"].rstrip("/")
    api_key = ctx["client_snippet_api_key"]
    model_alias = ctx["downstream_model_alias"]
    openai_base = base_url + "/v1"
    anthropic_base = base_url + "/anthropic"
    codex_config_toml = (
        'model_provider = "gateway"\n'
        f'model = "{_toml_string(model_alias)}"\n'
        f'model_reasoning_effort = "{_toml_string(ctx["codex_reasoning_effort"])}"\n'
        f'model_context_window = {int(ctx["client_context_window"])}\n'
        f'model_max_output_tokens = {int(ctx["client_output_token_limit"])}\n'
        "\n"
        "[model_providers.gateway]\n"
        'name = "gateway"\n'
        f'base_url = "{_toml_string(openai_base)}"\n'
        'env_key = "OPENAI_API_KEY"\n'
        'wire_api = "responses"\n'
    )
    codex_auth_json = json.dumps({"OPENAI_API_KEY": api_key}, ensure_ascii=False, indent=2)
    opencode_json = json.dumps({"provider": {"gateway": {"baseURL": openai_base, "apiKey": api_key}}, "model": model_alias}, ensure_ascii=False, indent=2)
    claude_bash_profile_function = (
        "claude_mnative() {\n"
        f'  export ANTHROPIC_BASE_URL="{anthropic_base}"\n'
        "  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\n"
        f'  export ANTHROPIC_AUTH_TOKEN="{api_key}"\n'
        '  export ANTHROPIC_API_KEY=""\n'
        f'  export ANTHROPIC_DEFAULT_OPUS_MODEL="{model_alias}"\n'
        f'  export ANTHROPIC_DEFAULT_SONNET_MODEL="{model_alias}"\n'
        f'  export ANTHROPIC_DEFAULT_HAIKU_MODEL="{model_alias}"\n'
        f'  export ANTHROPIC_MODEL="{model_alias}"\n'
        f'  export ANTHROPIC_SMALL_FAST_MODEL="{model_alias}"\n'
        "  export ENABLE_LSP_TOOL=1\n"
        '  local claude_bin="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"\n'
        '  if [ -z "$claude_bin" ]; then echo "Claude binary not found; set CLAUDE_BIN" >&2; return 127; fi\n'
        '  "$claude_bin" --dangerously-skip-permissions "$@"\n'
        "}\n"
    )
    vscode_claude_settings_json = json.dumps({"ANTHROPIC_BASE_URL": anthropic_base, "ANTHROPIC_AUTH_TOKEN": api_key, "ANTHROPIC_API_KEY": ""}, ensure_ascii=False, indent=2)
    return {
        "openai_base_url": openai_base,
        "anthropic_base_url": anthropic_base,
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
            "ANTHROPIC_BASE_URL": anthropic_base,
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_API_KEY": "",
        },
        "opencode_env": {
            "OPENAI_BASE_URL": openai_base,
            "OPENAI_API_KEY": api_key,
        },
    }


def _render_client_config_ui() -> str:
    ctx = _client_snippet_context()
    base_url = html.escape(ctx['public_base_url'].rstrip('/'))
    anthropic_base_url = html.escape(ctx['public_base_url'].rstrip('/') + '/anthropic')
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
export ANTHROPIC_AUTH_TOKEN="%s"
export ANTHROPIC_API_KEY=""
claude --dangerously-skip-permissions</code></pre>
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
</html>""" % (anthropic_base_url, api_key, base_url, api_key, base_url, api_key)


def _render_admin_ui() -> str:
    from .gateway_config import load_config, _redacted_config
    from .gateway_logging import _stats_snapshot, _tail_requests, _tail_failures, _tool_catalog_snapshot
    from .gateway_context import _sqlite_tail_memories
    from .gateway_agent_planner import list_runtime_events
    from .gateway_mcp import _enabled_mcp_servers, _mcp_list_server_tools, _mcp_public_name

    cfg = load_config()
    redacted = _redacted_config(cfg)
    stats = _stats_snapshot()
    requests = _tail_requests(20)
    failures = _tail_failures(20)
    memories = _sqlite_tail_memories(20)
    tools = _tool_catalog_snapshot()
    mcp_servers = _enabled_mcp_servers()

    upstream = cfg.get("upstream", {}) if isinstance(cfg.get("upstream"), dict) else {}
    upstream_profiles = cfg.get("upstream_profiles") if isinstance(cfg.get("upstream_profiles"), list) else []
    active_upstream_id = str(cfg.get("active_upstream_id") or cfg.get("active_upstream") or upstream.get("id") or "")
    downstream_keys = cfg.get("downstream_keys") if isinstance(cfg.get("downstream_keys"), list) else []
    gateway_cfg = cfg.get("gateway", {}) if isinstance(cfg.get("gateway"), dict) else {}
    context_cfg = cfg.get("context", {}) if isinstance(cfg.get("context"), dict) else {}
    caps = upstream.get("capabilities", {}) if isinstance(upstream.get("capabilities"), dict) else {}
    paths = upstream.get("paths", {}) if isinstance(upstream.get("paths"), dict) else {}

    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""), quote=True)

    def checked(value: Any) -> str:
        return " checked" if bool(value) else ""

    def selected(current: Any, value: str) -> str:
        return " selected" if str(current or "") == value else ""

    def badge(label: str, ok: Any, *, warn: bool = False) -> str:
        cls = "badge-ok" if ok else ("badge-warn" if warn else "badge-muted")
        text = "on" if ok else "off"
        return f'<span class="badge {cls}">{esc(label)} · {text}</span>'

    def option_rows(items: list[tuple[str, str]], current: Any) -> str:
        return "".join(f'<option value="{esc(value)}"{selected(current, value)}>{esc(label)}</option>' for label, value in items)

    capability_specs = [
        ("cap_supports_tools", "supports_tools", "Tools", "上游原生 tools/tool_calls"),
        ("cap_supports_function_calls", "supports_function_calls", "Function calls", "OpenAI function/tool call 对象"),
        ("cap_supports_parallel_tool_calls", "supports_parallel_tool_calls", "Parallel tools", "并行工具调用"),
        ("cap_supports_vision", "supports_vision", "Vision / 识图", "图片/截图输入"),
        ("cap_supports_streaming", "supports_streaming", "Streaming", "流式输出"),
        ("cap_supports_json_schema", "supports_json_schema", "JSON schema", "严格参数 schema"),
        ("cap_supports_network", "supports_network", "Network", "模型侧联网能力"),
        ("cap_supports_web_search", "supports_web_search", "Web search", "模型侧搜索能力"),
    ]
    capability_inputs = "\n".join(
        '<label class="check-card"><input type="checkbox" name="%s" value="1"%s><span><b>%s</b><small>%s</small></span></label>'
        % (form_key, checked(caps.get(cap_key)), esc(label), esc(desc))
        for form_key, cap_key, label, desc in capability_specs
    )
    capability_badges = " ".join(badge(label, caps.get(cap_key)) for _, cap_key, label, _ in capability_specs[:6])

    profile_rows = []
    for p_item in upstream_profiles:
        if not isinstance(p_item, dict):
            continue
        pcaps = p_item.get("capabilities", {}) if isinstance(p_item.get("capabilities"), dict) else {}
        is_active = str(p_item.get("id") or "") == active_upstream_id
        active_label = '<span class="pill active">Active</span>' if is_active else '<span class="pill">Standby</span>'
        cap_summary = " ".join([
            badge("tools", pcaps.get("supports_tools")),
            badge("vision", pcaps.get("supports_vision")),
            badge("stream", pcaps.get("supports_streaming")),
        ])
        profile_rows.append(
            "<tr%s>" % (' class="active-row"' if is_active else "")
            + f"<td>{active_label}<div class='mono muted'>{esc(p_item.get('id'))}</div></td>"
            + f"<td><b>{esc(p_item.get('name'))}</b><div class='muted'>{esc(p_item.get('base_url'))}</div></td>"
            + f"<td><span class='mono'>{esc(p_item.get('model'))}</span></td>"
            + f"<td>{esc(p_item.get('protocol'))}<div class='muted'>tools: {esc(p_item.get('tools_enabled'))}</div></td>"
            + f"<td>{cap_summary}</td>"
            + "<td class='actions'>"
            + '<form method="POST" action="/admin/upstream-profile"><input type="hidden" name="action" value="activate">'
            + f'<input type="hidden" name="id" value="{esc(p_item.get("id"))}"><button type="submit">Activate</button></form>'
            + '<form method="POST" action="/admin/upstream-profile"><input type="hidden" name="action" value="delete">'
            + f'<input type="hidden" name="id" value="{esc(p_item.get("id"))}"><button type="submit" class="danger">Delete</button></form>'
            + "</td></tr>"
        )

    key_rows = []
    for k_item in downstream_keys:
        if not isinstance(k_item, dict):
            continue
        key_rows.append(
            "<tr>"
            + f"<td><b>{esc(k_item.get('name'))}</b></td>"
            + f"<td><span class='mono'>{esc(k_item.get('prefix'))}</span></td>"
            + f"<td>{badge('enabled', k_item.get('enabled', True))}</td>"
            + "<td class='actions'><form method='POST' action='/admin/downstream-key'><input type='hidden' name='action' value='delete'>"
            + f"<input type='hidden' name='name' value='{esc(k_item.get('name'))}'><button type='submit' class='danger'>Delete</button></form></td>"
            + "</tr>"
        )

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

    mcp_rows = []
    for s_item in mcp_servers:
        tool_count = len([t for t in mcp_tools if t.get("server") == s_item.get("name") and not t.get("error")])
        mcp_rows.append(
            "<tr>"
            + f"<td><b>{esc(s_item.get('name'))}</b></td>"
            + f"<td>{badge('enabled', s_item.get('enabled', True))}</td>"
            + f"<td>{tool_count}</td>"
            + "<td class='actions'><form method='POST' action='/admin/mcp'><input type='hidden' name='action' value='delete'>"
            + f"<input type='hidden' name='name' value='{esc(s_item.get('name'))}'><button type='submit' class='danger'>Delete</button></form></td>"
            + "</tr>"
        )

    tool_items = tools.get("tools", [])
    if isinstance(tool_items, dict):
        tool_items = list(tool_items.values())
    if not isinstance(tool_items, list):
        tool_items = []
    tool_name_count = len(tool_items)
    unique_tool_count = len({str(t.get("canonical_name") or t.get("name") or "") for t in tool_items if isinstance(t, dict) and (t.get("canonical_name") or t.get("name"))})
    tool_rows = []
    for t_item in tool_items[:80]:
        if not isinstance(t_item, dict):
            continue
        tool_rows.append(
            "<tr>"
            + f"<td><span class='mono'>{esc(t_item.get('name'))}</span></td>"
            + f"<td>{esc(t_item.get('canonical_name') or t_item.get('name'))}</td>"
            + f"<td>{esc(t_item.get('risk'))}</td>"
            + f"<td>{esc(str(t_item.get('description') or '')[:120])}</td>"
            + "</tr>"
        )

    request_rows = []
    for r_item in requests[:10]:
        request_rows.append(
            "<tr>"
            + f"<td class='mono'>{esc(str(r_item.get('ts') or '')[:19])}</td>"
            + f"<td><span class='mono'>{esc(r_item.get('path'))}</span></td>"
            + f"<td>{esc(r_item.get('status', ''))}</td>"
            + "</tr>"
        )

    failure_rows = []
    for f_item in failures[:10]:
        failure_rows.append(
            "<tr>"
            + f"<td class='mono'>{esc(str(f_item.get('ts') or '')[:19])}</td>"
            + f"<td>{esc(f_item.get('tool_name'))}</td>"
            + f"<td>{esc(f_item.get('failure_type'))}</td>"
            + f"<td>{esc(str(f_item.get('content') or '')[:100])}</td>"
            + "</tr>"
        )

    memory_rows = []
    for m_item in memories[:10]:
        memory_rows.append(
            "<tr>"
            + f"<td class='mono'>{esc(str(m_item.get('ts') or '')[:19])}</td>"
            + f"<td class='mono'>{esc(str(m_item.get('session_key') or '')[:12])}</td>"
            + f"<td>{esc(m_item.get('kind'))}</td>"
            + f"<td>{esc(str(m_item.get('summary') or '')[:120])}</td>"
            + "</tr>"
        )

    total_requests = stats.get("total_requests") or (stats.get("requests", {}) or {}).get("total") or 0
    failure_count = len(failures)
    active_model = upstream.get("model") or gateway_cfg.get("downstream_model_alias") or ""
    public_base = str(gateway_cfg.get("public_base_url") or "http://127.0.0.1:8885").rstrip("/")
    snippet_ctx = _client_config_snippets()
    claude_function_pretty = esc(snippet_ctx.get("claude_bash_profile_function") or "")
    config_pretty = html.escape(json.dumps(redacted, indent=2, ensure_ascii=False))
    stats_pretty = html.escape(json.dumps(stats, indent=2, ensure_ascii=False))



    # --- Skills data ---
    try:
        from .gateway_builtin_tools import _skill_dirs, _load_skill
        _skill_dirs_list = _skill_dirs()
    except Exception:
        _skill_dirs_list = []

    skill_items = []
    for root in _skill_dirs_list:
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob('*/SKILL.md')):
            skill_name = skill_file.parent.name
            skill_items.append({'name': skill_name, 'path': str(skill_file), 'source': str(root)})

    # --- HTTP Actions ---
    try:
        from .gateway_http_actions import _enabled_http_actions
        http_actions = _enabled_http_actions()
    except Exception:
        http_actions = []


    total_requests = stats.get("total_requests") or (stats.get("requests", {}) or {}).get("total") or 0
    failure_count = len(failures)
    active_model = upstream.get("model") or gateway_cfg.get("downstream_model_alias") or ""
    public_base = str(gateway_cfg.get("public_base_url") or "http://127.0.0.1:8885").rstrip("/")
    snippet_ctx = _client_config_snippets()
    claude_function_pretty = esc(snippet_ctx.get("claude_bash_profile_function") or "")
    config_pretty = html.escape(json.dumps(redacted, indent=2, ensure_ascii=False))
    stats_pretty = html.escape(json.dumps(stats, indent=2, ensure_ascii=False))

    profile_rows = []
    for p_item in upstream_profiles:
        if not isinstance(p_item, dict):
            continue
        pcaps = p_item.get("capabilities", {}) if isinstance(p_item.get("capabilities"), dict) else {}
        is_active = str(p_item.get("id") or "") == active_upstream_id
        active_label = '<span class="pill active">Active</span>' if is_active else '<span class="pill">Standby</span>'
        cap_summary = " ".join([
            badge("tools", pcaps.get("supports_tools")),
            badge("vision", pcaps.get("supports_vision")),
            badge("stream", pcaps.get("supports_streaming")),
        ])
        profile_rows.append(
            "<tr%s>" % (' class="active-row"' if is_active else "")
            + f"<td>{active_label}<div class='mono muted small'>{esc(p_item.get('id'))}</div></td>"
            + f"<td><b>{esc(p_item.get('name'))}</b><div class='muted small'>{esc(p_item.get('base_url'))}</div></td>"
            + f"<td><span class='mono'>{esc(p_item.get('model'))}</span></td>"
            + f"<td>{esc(p_item.get('protocol'))}<div class='muted small'>tools: {esc(p_item.get('tools_enabled'))}</div></td>"
            + f"<td>{cap_summary}</td>"
            + "<td class='actions'>"
            + '<form method="POST" action="/admin/upstream-profile"><input type="hidden" name="action" value="activate">'
            + f'<input type="hidden" name="id" value="{esc(p_item.get("id"))}"><button type="submit" class="sm">Activate</button></form>'
            + '<form method="POST" action="/admin/upstream-profile"><input type="hidden" name="action" value="delete">'
            + f'<input type="hidden" name="id" value="{esc(p_item.get("id"))}"><button type="submit" class="sm danger">Delete</button></form>'
            + "</td></tr>"
        )

    key_rows = []
    for k_item in downstream_keys:
        if not isinstance(k_item, dict):
            continue
        key_rows.append(
            "<tr>"
            + f"<td><b>{esc(k_item.get('name'))}</b></td>"
            + f"<td><span class='mono'>{esc(k_item.get('prefix'))}</span></td>"
            + f"<td>{badge('enabled', k_item.get('enabled', True))}</td>"
            + "<td class='actions'><form method='POST' action='/admin/downstream-key'><input type='hidden' name='action' value='delete'>"
            + f"<input type='hidden' name='name' value='{esc(k_item.get('name'))}'><button type='submit' class='sm danger'>Delete</button></form></td>"
            + "</tr>"
        )

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

    mcp_rows = []
    for s_item in mcp_servers:
        tool_count = len([t for t in mcp_tools if t.get("server") == s_item.get("name") and not t.get("error")])
        mcp_rows.append(
            "<tr>"
            + f"<td><b>{esc(s_item.get('name'))}</b></td>"
            + f"<td>{badge('enabled', s_item.get('enabled', True))}</td>"
            + f"<td>{tool_count}</td>"
            + "<td class='actions'><form method='POST' action='/admin/mcp'><input type='hidden' name='action' value='delete'>"
            + f"<input type='hidden' name='name' value='{esc(s_item.get('name'))}'><button type='submit' class='sm danger'>Delete</button></form></td>"
            + "</tr>"
        )

    tool_items = tools.get("tools", [])
    if isinstance(tool_items, dict):
        tool_items = list(tool_items.values())
    if not isinstance(tool_items, list):
        tool_items = []
    tool_name_count = len(tool_items)
    unique_tool_count = len({str(t.get("canonical_name") or t.get("name") or "") for t in tool_items if isinstance(t, dict) and (t.get("canonical_name") or t.get("name"))})
    tool_rows = []
    for t_item in tool_items[:80]:
        if not isinstance(t_item, dict):
            continue
        tool_rows.append(
            "<tr>"
            + f"<td><span class='mono'>{esc(t_item.get('name'))}</span></td>"
            + f"<td>{esc(t_item.get('canonical_name') or t_item.get('name'))}</td>"
            + f"<td>{esc(t_item.get('risk'))}</td>"
            + f"<td class='desc-cell'>{esc(str(t_item.get('description') or '')[:120])}</td>"
            + "</tr>"
        )

    request_rows = []
    for r_item in requests[:15]:
        request_rows.append(
            "<tr>"
            + f"<td class='mono small'>{esc(str(r_item.get('ts') or '')[:19])}</td>"
            + f"<td><span class='mono'>{esc(r_item.get('path'))}</span></td>"
            + f"<td>{esc(r_item.get('status', ''))}</td>"
            + "</tr>"
        )

    failure_rows = []
    for f_item in failures[:15]:
        failure_rows.append(
            "<tr>"
            + f"<td class='mono small'>{esc(str(f_item.get('ts') or '')[:19])}</td>"
            + f"<td>{esc(f_item.get('tool_name'))}</td>"
            + f"<td>{esc(f_item.get('failure_type'))}</td>"
            + f"<td class='desc-cell'>{esc(str(f_item.get('content') or '')[:100])}</td>"
            + "</tr>"
        )

    memory_rows = []
    for m_item in memories[:15]:
        memory_rows.append(
            "<tr>"
            + f"<td class='mono small'>{esc(str(m_item.get('ts') or '')[:19])}</td>"
            + f"<td class='mono'>{esc(str(m_item.get('session_key') or '')[:12])}</td>"
            + f"<td>{esc(m_item.get('kind'))}</td>"
            + f"<td class='desc-cell'>{esc(str(m_item.get('summary') or '')[:120])}</td>"
            + "</tr>"
        )

    skill_rows = []
    for s_item in skill_items:
        skill_rows.append(
            "<tr>"
            + f"<td><b>{esc(s_item['name'])}</b></td>"
            + f"<td class='muted small'>{esc(s_item['source'])}</td>"
            + "<td class='actions'>"
            + f'<button type="button" class="sm" onclick="viewSkill(\'{esc(s_item["name"])}\')">View</button>'
            + "</td></tr>"
        )

    action_rows = []
    for a_item in http_actions:
        action_rows.append(
            "<tr>"
            + f"<td><b>{esc(a_item.get('name'))}</b></td>"
            + f"<td><span class='mono'>{esc(str(a_item.get('method') or 'POST'))}</span></td>"
            + f"<td class='mono small'>{esc(a_item.get('url'))}</td>"
            + f"<td class='desc-cell'>{esc(str(a_item.get('description') or '')[:80])}</td>"
            + f"<td>{badge('enabled', a_item.get('enabled', True))}</td>"
            + "</tr>"
        )

    capability_specs = [
        ("cap_supports_tools", "supports_tools", "Tools", "上游原生 tools/tool_calls"),
        ("cap_supports_function_calls", "supports_function_calls", "Function calls", "OpenAI function/tool call 对象"),
        ("cap_supports_parallel_tool_calls", "supports_parallel_tool_calls", "Parallel tools", "并行工具调用"),
        ("cap_supports_vision", "supports_vision", "Vision / 识图", "图片/截图输入"),
        ("cap_supports_streaming", "supports_streaming", "Streaming", "流式输出"),
        ("cap_supports_json_schema", "supports_json_schema", "JSON schema", "严格参数 schema"),
        ("cap_supports_network", "supports_network", "Network", "模型侧联网能力"),
        ("cap_supports_web_search", "supports_web_search", "Web search", "模型侧搜索能力"),
    ]
    capability_inputs = "\n".join(
        '<label class="check-card"><input type="checkbox" name="%s" value="1"%s><span><b>%s</b><small>%s</small></span></label>'
        % (form_key, checked(caps.get(cap_key)), esc(label), esc(desc))
        for form_key, cap_key, label, desc in capability_specs
    )

    return _render_admin_html(
        esc=esc, badge=badge, checked=checked,
        upstream=upstream, upstream_profiles=upstream_profiles,
        active_upstream_id=active_upstream_id,
        downstream_keys=downstream_keys, gateway_cfg=gateway_cfg,
        context_cfg=context_cfg, caps=caps, paths=paths,
        profile_rows=profile_rows, key_rows=key_rows,
        mcp_rows=mcp_rows, tool_rows=tool_rows,
        tool_name_count=tool_name_count, unique_tool_count=unique_tool_count,
        request_rows=request_rows, failure_rows=failure_rows,
        memory_rows=memory_rows, skill_rows=skill_rows,
        action_rows=action_rows, capability_inputs=capability_inputs,
        capability_specs=capability_specs,
        total_requests=total_requests, failure_count=failure_count,
        active_model=active_model, public_base=public_base,
        snippet_ctx=snippet_ctx, claude_function_pretty=claude_function_pretty,
        config_pretty=config_pretty, stats_pretty=stats_pretty,
        skill_items=skill_items, mcp_servers=mcp_servers,
        skill_count=len(skill_items),
    )


# ============================================================================
# Redesigned UI: Black & White Clean Design
# ============================================================================

import json as _json_mod
import html as _html_mod
import pathlib as _pathlib_mod
import os as _os_mod
import re as _re_mod

_ADMIN_CSS = '@import url(\'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap\');\n:root{--bg:#fff;--bg2:#f8f9fa;--bg3:#f0f0f0;--fg:#111;--fg2:#333;--fg3:#666;--muted:#999;--border:#e0e0e0;--border2:#ccc;--accent:#111;--link:#0066cc;--ok:#16a34a;--warn:#d97706;--err:#dc2626;--info:#2563eb;--radius:8px}\n*{box-sizing:border-box;margin:0;padding:0}\nbody{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);line-height:1.5;font-size:14px}\na{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}\n.shell{max-width:1280px;margin:0 auto;padding:20px 24px}\n.header{display:flex;align-items:center;justify-content:space-between;padding:16px 0;border-bottom:2px solid var(--fg);margin-bottom:20px}\n.header h1{font-size:20px;font-weight:700}\n.header .subtitle{color:var(--muted);font-size:12px;margin-top:2px}\n.header .actions{display:flex;gap:8px}\n.stats-bar{display:flex;gap:1px;background:var(--border);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:20px}\n.stats-bar .stat{flex:1;background:var(--bg);padding:12px 16px;text-align:center}\n.stats-bar .stat .label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}\n.stats-bar .stat .value{font-size:20px;font-weight:700;margin-top:2px}\n.tabs{display:flex;gap:0;border-bottom:2px solid var(--fg);margin-bottom:20px}\n.tab{padding:10px 18px;font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .12s;user-select:none}\n.tab:hover{color:var(--fg2)}.tab.active{color:var(--fg);border-bottom-color:var(--fg)}\n.tab-content{display:none;animation:fadeIn .15s}.tab-content.active{display:block}\n@keyframes fadeIn{from{opacity:0}to{opacity:1}}\n.card{border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:16px;background:var(--bg)}\n.card h2{font-size:15px;font-weight:700;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)}\n.card h3{font-size:13px;font-weight:600;margin:12px 0 6px;color:var(--fg2)}\n.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}\ntable{width:100%;border-collapse:collapse;font-size:12px}\nth,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}\nth{font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--fg3);background:var(--bg2);position:sticky;top:0}\ntr:hover{background:var(--bg2)}.tr.active-row{background:#f0fdf4}\n.info-table{display:grid;grid-template-columns:130px 1fr;gap:0;border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;font-size:13px}\n.info-table .k{padding:8px 12px;background:var(--bg2);color:var(--fg3);font-weight:500;border-bottom:1px solid var(--border)}\n.info-table .v{padding:8px 12px;border-bottom:1px solid var(--border);word-break:break-all}\n.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;border:1px solid var(--border)}\n.badge-ok{color:var(--ok);border-color:var(--ok);background:#f0fdf4}\n.badge-err{color:var(--err);border-color:var(--err);background:#fef2f2}\n.badge-warn{color:var(--warn);border-color:var(--warn);background:#fffbeb}\n.badge-muted{color:var(--muted)}\n.badge-wrap{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}\n.form-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}\n.form-row .wide{grid-column:span 2}.form-row .full{grid-column:1/-1}\nlabel.field{display:flex;flex-direction:column;gap:3px;font-size:12px;font-weight:500;color:var(--fg2)}\ninput,select,textarea{width:100%;border:1px solid var(--border);border-radius:6px;padding:7px 9px;font-size:13px;background:var(--bg);color:var(--fg);outline:none;font-family:inherit}\ntextarea{min-height:80px;resize:vertical}\ninput:focus,select:focus,textarea:focus{border-color:var(--fg);box-shadow:0 0 0 1px var(--fg)}\nbutton,.btn{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--fg);border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer;background:var(--fg);color:var(--bg);transition:opacity .12s;font-family:inherit}\nbutton:hover{opacity:.85}\nbutton.sm{padding:4px 8px;font-size:11px}\nbutton.ghost{background:transparent;color:var(--fg)}.button.ghost:hover{background:var(--bg2)}\nbutton.danger{background:var(--err);border-color:var(--err)}\nbutton.ok{background:var(--ok);border-color:var(--ok)}\npre{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:12px;overflow-x:auto;font-size:12px;line-height:1.5}\npre code{font-family:"SF Mono",Menlo,Consolas,monospace;color:var(--fg2);white-space:pre}\n.mono{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12px}\n.muted{color:var(--muted)}.small{font-size:12px}\n.actions{display:flex;gap:6px;flex-wrap:wrap}td form{display:inline-flex;margin:0}\n.check-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px}\n.check-card{display:flex;gap:6px;align-items:flex-start;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer}\n.check-card:hover{border-color:var(--fg2)}\n.check-card input{width:auto;margin-top:2px}\n.check-card span{display:flex;flex-direction:column;gap:1px}\n.check-card b{font-size:11px}.check-card small{color:var(--muted);font-size:10px}\n.search-bar{margin-bottom:12px}.search-bar input{border-radius:6px;padding:8px 12px;font-size:13px}\n.skill-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}\n.skill-card{border:1px solid var(--border);border-radius:var(--radius);padding:14px;transition:border-color .12s}\n.skill-card:hover{border-color:var(--fg2)}\n.skill-card h4{font-size:13px;margin-bottom:4px}\n.skill-card .meta{font-size:11px;color:var(--muted);margin-bottom:6px}\n.todo-item{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;margin-bottom:8px}\n.todo-item .icon{font-size:16px;flex-shrink:0;margin-top:1px}\n.todo-item .body{flex:1}.todo-item .body h4{font-size:12px;margin-bottom:2px}.todo-item .body p{font-size:11px;color:var(--muted);margin:0}\n.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:1000;justify-content:center;align-items:center}\n.modal-overlay.show{display:flex}\n.modal{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:800px;max-height:80vh;overflow-y:auto;width:90%;box-shadow:0 20px 60px rgba(0,0,0,.2)}\n.modal h2{margin-bottom:10px;font-size:16px}.modal pre{max-height:55vh}\n.modal-close{float:right;background:none;border:0;color:var(--muted);font-size:20px;cursor:pointer;padding:0 4px}\n.modal-close:hover{color:var(--fg)}\n.loading{color:var(--muted);font-size:13px;padding:20px;text-align:center}\n@media(max-width:900px){.grid2{grid-template-columns:1fr}.tabs{overflow-x:auto}.tab{padding:8px 12px;font-size:12px;white-space:nowrap}.stats-bar{flex-wrap:wrap}.stats-bar .stat{min-width:100px}}'
_ADMIN_JS = '(function(){\n  document.querySelectorAll(\'.tab\').forEach(function(tab){\n    tab.addEventListener(\'click\',function(){\n      document.querySelectorAll(\'.tab\').forEach(function(t){t.classList.remove(\'active\')});\n      document.querySelectorAll(\'.tab-content\').forEach(function(p){p.classList.remove(\'active\')});\n      tab.classList.add(\'active\');\n      var panel=document.querySelector(\'[data-panel="\'+tab.dataset.tab+\'"]\');\n      if(panel)panel.classList.add(\'active\');\n      history.replaceState(null,\'\',\'#\'+tab.dataset.tab);\n    });\n  });\n  var hash=location.hash.replace(\'#\',\'\');\n  if(hash){var t=document.querySelector(\'.tab[data-tab="\'+hash+\'"]\');if(t)t.click();}\n  else{var first=document.querySelector(\'.tab\');if(first)first.classList.add(\'active\');}\n})();\nfunction copyText(btn,text){\n  navigator.clipboard.writeText(text).then(function(){\n    var o=btn.textContent;btn.textContent=\'Copied!\';setTimeout(function(){btn.textContent=o;},1200);\n  });\n}\nfunction viewSkill(name){\n  var modal=document.getElementById(\'skill-modal\');\n  document.getElementById(\'skill-modal-title\').textContent=name;\n  document.getElementById(\'skill-modal-path\').textContent=\'Loading...\';\n  document.getElementById(\'skill-modal-content\').textContent=\'Loading...\';\n  modal.classList.add(\'show\');\n  fetch(\'/admin/skill-content.json?name=\'+encodeURIComponent(name)).then(function(r){return r.json();}).then(function(d){\n    document.getElementById(\'skill-modal-path\').textContent=d.path||\'\';\n    document.getElementById(\'skill-modal-content\').textContent=d.content||\'No content\';\n  }).catch(function(e){\n    document.getElementById(\'skill-modal-content\').textContent=\'Error: \'+e.message;\n  });\n}\nfunction closeSkillModal(){document.getElementById(\'skill-modal\').classList.remove(\'show\');}\ndocument.getElementById(\'skill-modal\').addEventListener(\'click\',function(e){if(e.target===this)closeSkillModal();});\ndocument.addEventListener(\'keydown\',function(e){if(e.key===\'Escape\')closeSkillModal();});\nfunction fetchModels(){\n  var st=document.getElementById(\'model-fetch-status\');\n  var dl=document.getElementById(\'upstream-model-options\');\n  var mdl=document.getElementById(\'model\');\n  st.textContent=\'fetching...\';\n  var params=new URLSearchParams();\n  [\'base_url\',\'api_key\',\'protocol\',\'path_models\'].forEach(function(id){\n    var el=document.getElementById(id);if(el&&el.value)params.set(id,el.value);\n  });\n  fetch(\'/admin/upstream-models.json\',{method:\'POST\',headers:{\'accept\':\'application/json\',\'content-type\':\'application/x-www-form-urlencoded\'},body:params.toString()}).then(function(r){return r.json();}).then(function(p){\n    if(!p.ok)throw new Error(p.error||\'failed\');\n    dl.innerHTML=\'\';\n    (p.models||[]).forEach(function(m){var o=document.createElement(\'option\');o.value=m;dl.appendChild(o);});\n    if(!mdl.value&&p.models&&p.models.length)mdl.value=p.models[0];\n    st.textContent=\'loaded \'+(p.models||[]).length+\' models\';\n  }).catch(function(e){st.textContent=\'Error: \'+e.message;});\n}\nfunction installSkill(id){\n  fetch(\'/admin/skill-install.json\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({id:id})}).then(function(r){return r.json();}).then(function(d){\n    if(d.ok){alert(\'Installed: \'+id);location.reload();}else{alert(\'Error: \'+(d.error||\'unknown\'));}\n  }).catch(function(e){alert(\'Error: \'+e.message);});\n}\nfunction installMCP(id){\n  fetch(\'/admin/mcp-install.json\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({id:id})}).then(function(r){return r.json();}).then(function(d){\n    if(d.ok){alert(\'Added: \'+id);location.reload();}else{alert(\'Error: \'+(d.error||\'unknown\'));}\n  }).catch(function(e){alert(\'Error: \'+e.message);});\n}\n(function(){\n  var s=document.getElementById(\'skill-search\');\n  if(s)s.addEventListener(\'input\',function(){var q=this.value.toLowerCase();document.querySelectorAll(\'.skill-card\').forEach(function(c){c.style.display=c.textContent.toLowerCase().indexOf(q)>=0?\'\':\'none\';});});\n  var t=document.getElementById(\'tool-search\');\n  if(t)t.addEventListener(\'input\',function(){var q=this.value.toLowerCase();document.querySelectorAll(\'#tool-tbody tr\').forEach(function(r){r.style.display=r.textContent.toLowerCase().indexOf(q)>=0?\'\':\'none\';});});\n})();'

def _load_static_assets():
    """Load CSS and JS from inline constants."""
    global _ADMIN_CSS, _ADMIN_JS
    # CSS and JS are set at module load time below


def _render_admin_ui() -> str:
    """Render the admin UI with all data."""
    from .gateway_config import load_config, _redacted_config
    from .gateway_logging import _stats_snapshot, _tail_requests, _tail_failures, _tool_catalog_snapshot
    from .gateway_context import _sqlite_tail_memories
    from .gateway_agent_planner import list_runtime_events
    from .gateway_mcp import _enabled_mcp_servers, _mcp_list_server_tools, _mcp_public_name

    cfg = load_config()
    redacted = _redacted_config(cfg)
    stats = _stats_snapshot()
    requests = _tail_requests(30)
    failures = _tail_failures(30)
    memories = _sqlite_tail_memories(30)
    runtime_events = list_runtime_events(30)
    tools = _tool_catalog_snapshot()
    mcp_servers = _enabled_mcp_servers()

    upstream = cfg.get("upstream", {}) if isinstance(cfg.get("upstream"), dict) else {}
    upstream_profiles = cfg.get("upstream_profiles") if isinstance(cfg.get("upstream_profiles"), list) else []
    active_upstream_id = str(cfg.get("active_upstream_id") or cfg.get("active_upstream") or upstream.get("id") or "")
    downstream_keys = cfg.get("downstream_keys") if isinstance(cfg.get("downstream_keys"), list) else []
    gateway_cfg = cfg.get("gateway", {}) if isinstance(cfg.get("gateway"), dict) else {}
    context_cfg = cfg.get("context", {}) if isinstance(cfg.get("context"), dict) else {}
    caps = upstream.get("capabilities", {}) if isinstance(upstream.get("capabilities"), dict) else {}
    paths = upstream.get("paths", {}) if isinstance(upstream.get("paths"), dict) else {}

    tool_items = tools.get("tools", [])
    if isinstance(tool_items, dict):
        tool_items = list(tool_items.values())
    if not isinstance(tool_items, list):
        tool_items = []
    unique_tools = len({str(t.get("canonical_name") or t.get("name") or "") for t in tool_items if isinstance(t, dict) and (t.get("canonical_name") or t.get("name"))})

    from .gateway_builtin_tools import _skill_dirs
    skills = []
    for root in _skill_dirs():
        if not root.is_dir():
            continue
        for sf in sorted(root.glob("*/SKILL.md")):
            skills.append({"name": sf.parent.name, "path": str(sf), "source": str(root)})

    mcp_tools = []
    for s in mcp_servers:
        sname = str(s.get("name") or "")
        try:
            for t in _mcp_list_server_tools(s):
                mcp_tools.append({"server": sname, "name": t.get("name"), "gateway_name": _mcp_public_name(sname, str(t.get("name"))), "description": t.get("description")})
        except Exception as e:
            mcp_tools.append({"server": sname, "error": str(e)})

    from .gateway_http_actions import _enabled_http_actions
    http_actions = []
    for a in _enabled_http_actions():
        http_actions.append({"name": a.get("name"), "method": str(a.get("method") or "POST").upper(), "url": a.get("url"), "description": a.get("description"), "enabled": a.get("enabled", True)})

    total_requests = stats.get("total_requests", 0)
    failure_count = stats.get("failure_count", 0)

    return _render_html(
        upstream=upstream, upstream_profiles=upstream_profiles,
        active_upstream_id=active_upstream_id, downstream_keys=downstream_keys,
        gateway_cfg=gateway_cfg, context_cfg=context_cfg, caps=caps, paths=paths,
        stats=stats, redacted=redacted, tool_items=tool_items,
        unique_tool_count=unique_tools, tool_name_count=len(tool_items),
        mcp_servers=mcp_servers, mcp_tools=mcp_tools, skills=skills,
        http_actions=http_actions, requests_data=requests, failures_data=failures,
        memories_data=memories, runtime_events_data=runtime_events, total_requests=total_requests,
        failure_count=failure_count,
        public_base=gateway_cfg.get("public_base_url", "http://127.0.0.1:8885"),
    )


def _esc(v):
    return _html_mod.escape(str(v if v is not None else ""))


def _badge(label, ok, *, warn=False):
    cls = "badge-ok" if ok else ("badge-warn" if warn else "badge-muted")
    return f'<span class="badge {cls}">{_esc(label)}</span>'


def _checked(v):
    return " checked" if bool(v) else ""


def _sel(cur, val):
    return " selected" if str(cur or "") == val else ""


def _render_html(**kw):
    """Render the full HTML page."""
    upstream = kw["upstream"]
    upstream_profiles = kw["upstream_profiles"]
    active_upstream_id = kw["active_upstream_id"]
    downstream_keys = kw["downstream_keys"]
    gateway_cfg = kw["gateway_cfg"]
    context_cfg = kw["context_cfg"]
    caps = kw["caps"]
    paths = kw["paths"]
    stats = kw["stats"]
    redacted = kw["redacted"]
    tool_items = kw["tool_items"]
    unique_tool_count = kw["unique_tool_count"]
    tool_name_count = kw["tool_name_count"]
    mcp_servers = kw["mcp_servers"]
    mcp_tools = kw["mcp_tools"]
    skills = kw["skills"]
    http_actions = kw["http_actions"]
    requests_data = kw["requests_data"]
    failures_data = kw["failures_data"]
    memories_data = kw["memories_data"]
    runtime_events_data = kw.get("runtime_events_data") or []
    total_requests = kw["total_requests"]
    failure_count = kw["failure_count"]
    public_base = kw["public_base"]

    E = _esc
    B = _badge
    C = _checked
    S = _sel

    cap_specs = [("supports_tools","Tools"),("supports_function_calls","Function Calls"),("supports_parallel_tool_calls","Parallel Tools"),("supports_vision","Vision"),("supports_streaming","Streaming"),("supports_json_schema","JSON Schema"),("supports_network","Network"),("supports_web_search","Web Search")]
    cap_badges = " ".join(B(lab, caps.get(key)) for key, lab in cap_specs)

    cap_form = [("cap_supports_tools","supports_tools","Tools","Native tools"),("cap_supports_function_calls","supports_function_calls","Function Calls","OpenAI function_call"),("cap_supports_parallel_tool_calls","supports_parallel_tool_calls","Parallel","Parallel tool calls"),("cap_supports_vision","supports_vision","Vision","Image input"),("cap_supports_streaming","supports_streaming","Streaming","SSE"),("cap_supports_json_schema","supports_json_schema","JSON Schema","Structured output"),("cap_supports_network","supports_network","Network","Web search"),("cap_supports_web_search","supports_web_search","Web Search","Model search")]
    cap_inputs = "\n".join(f'<label class="check-card"><input type="checkbox" name="{fk}" value="1"{C(caps.get(ck))}><span><b>{lab}</b><small>{desc}</small></span></label>' for fk,ck,lab,desc in cap_form)

    # Profile rows
    prows = []
    for p in upstream_profiles:
        if not isinstance(p, dict): continue
        pcaps = p.get("capabilities", {}) if isinstance(p.get("capabilities"), dict) else {}
        act = str(p.get("id") or "") == active_upstream_id
        st = B("Active", True) if act else B("Standby", False)
        prows.append(f'<tr class="{"active-row" if act else ""}"><td>{st}</td><td><b>{E(p.get("name"))}</b><div class="mono small muted">{E(p.get("base_url"))}</div></td><td class="mono">{E(p.get("model"))}</td><td>{E(p.get("protocol"))}</td><td>{B("tools",pcaps.get("supports_tools"))} {B("vision",pcaps.get("supports_vision"))}</td><td class="actions"><form method="POST" action="/admin/upstream-profile"><input type="hidden" name="action" value="activate"><input type="hidden" name="id" value="{E(p.get("id"))}"><button class="sm">Activate</button></form><form method="POST" action="/admin/upstream-profile"><input type="hidden" name="action" value="delete"><input type="hidden" name="id" value="{E(p.get("id"))}"><button class="sm danger">Del</button></form></td></tr>')
    profile_html = "\n".join(prows) or '<tr><td colspan="6" class="muted">No profiles</td></tr>'

    krows = []
    for k in downstream_keys:
        if not isinstance(k, dict): continue
        krows.append(f'<tr><td><b>{E(k.get("name"))}</b></td><td class="mono">{E(k.get("prefix"))}</td><td>{B("enabled",k.get("enabled",True))}</td><td><form method="POST" action="/admin/downstream-key"><input type="hidden" name="action" value="delete"><input type="hidden" name="name" value="{E(k.get("name"))}"><button class="sm danger">Del</button></form></td></tr>')
    key_html = "\n".join(krows) or '<tr><td colspan="4" class="muted">No keys</td></tr>'

    mrows = []
    for s in mcp_servers:
        sn = str(s.get("name") or "")
        tc = len([t for t in mcp_tools if t.get("server") == sn and not t.get("error")])
        mrows.append(f'<tr><td><b>{E(sn)}</b></td><td>{B("enabled",s.get("enabled",True))}</td><td>{tc}</td><td><form method="POST" action="/admin/mcp"><input type="hidden" name="action" value="delete"><input type="hidden" name="name" value="{E(sn)}"><button class="sm danger">Del</button></form></td></tr>')
    mcp_html = "\n".join(mrows) or '<tr><td colspan="4" class="muted">No MCP servers</td></tr>'

    trows = []
    for t in tool_items[:100]:
        if not isinstance(t, dict): continue
        trows.append(f'<tr><td class="mono">{E(t.get("name"))}</td><td>{E(t.get("canonical_name") or t.get("name"))}</td><td>{E(t.get("risk"))}</td><td>{E(str(t.get("description") or "")[:100])}</td></tr>')
    tool_html = "\n".join(trows)

    arows = []
    for a in http_actions:
        arows.append(f'<tr><td><b>{E(a.get("name"))}</b></td><td class="mono">{E(a.get("method"))}</td><td class="mono small">{E(a.get("url"))}</td><td>{E(a.get("description"))}</td><td>{B("on" if a.get("enabled") else "off", a.get("enabled",True))}</td></tr>')
    action_html = "\n".join(arows) or '<tr><td colspan="5" class="muted">No HTTP actions</td></tr>'

    rrows = [f'<tr><td class="mono small">{E(str(r.get("ts",""))[:19])}</td><td class="mono">{E(r.get("path"))}</td><td>{E(r.get("status",""))}</td></tr>' for r in requests_data[:20]]
    req_html = "\n".join(rrows) or '<tr><td colspan="3" class="muted">No requests</td></tr>'
    frows = [f'<tr><td class="mono small">{E(str(f.get("ts",""))[:19])}</td><td>{E(f.get("tool_name"))}</td><td>{E(f.get("failure_type"))}</td><td>{E(str(f.get("content",""))[:80])}</td></tr>' for f in failures_data[:20]]
    fail_html = "\n".join(frows) or '<tr><td colspan="4" class="muted">No failures</td></tr>'
    mrows2 = [
        f'<tr><td class="mono small">{E(str(m.get("ts",""))[:19])}</td>'
        f'<td class="mono small">{E(str(m.get("tenant_key") or "")[:18])}</td>'
        f'<td class="mono small">{E(str(m.get("workspace_key") or m.get("workspace_root") or "")[-28:])}</td>'
        f'<td class="mono small">{E(str(m.get("memory_session_key") or m.get("session_key") or "")[:24])}</td>'
        f'<td>{E(m.get("kind"))}</td><td>{E(str(m.get("summary","") )[:100])}</td></tr>'
        for m in memories_data[:20]
    ]
    mem_html = "\n".join(mrows2) or '<tr><td colspan="6" class="muted">No memories</td></tr>'
    erows = [
        f'<tr><td class="mono small">{E(str(e.get("ts",""))[:19])}</td>'
        f'<td>{E(e.get("event_type"))}</td>'
        f'<td>{E(e.get("workflow"))}</td>'
        f'<td>{E(e.get("step"))}</td>'
        f'<td class="mono small">{E(str(e.get("tenant_key") or "")[:18])}</td>'
        f'<td class="mono small">{E(str(e.get("workspace_key") or "")[-28:])}</td>'
        f'<td>{E(str(e.get("summary",""))[:120])}</td></tr>'
        for e in runtime_events_data[:20]
    ]
    events_html = "\n".join(erows) or '<tr><td colspan="7" class="muted">No runtime events</td></tr>'

    scards = [f'<div class="skill-card"><h4>{E(s["name"])}</h4><div class="meta">{E(s["source"].split("/")[-1])}</div><div class="actions"><button class="sm ghost" onclick="viewSkill(\'{E(s["name"])}\')">View</button></div></div>' for s in skills]
    skill_html = "\n".join(scards) or '<p class="muted">No skills</p>'

    mcp_mkt = ""
    try:
        from .marketplace import list_mcp_marketplace
        mcp_mkt = "\n".join(f'<div class="skill-card"><h4>{E(i.get("name",""))}</h4><div class="meta">{E(", ".join(i.get("categories",[])))}</div><p class="small muted" style="margin-bottom:4px">{E(i.get("description",""))}</p><code class="mono small">{E(i.get("package",""))}</code><div class="actions" style="margin-top:6px"><button class="sm ok" onclick="installMCP(\'{E(i.get("id",""))}\')">Install</button></div></div>' for i in list_mcp_marketplace())
    except Exception:
        mcp_mkt = '<p class="muted">Marketplace unavailable</p>'

    sk_mkt = ""
    try:
        from .marketplace import list_skills_catalog
        sk_mkt = "\n".join(f'<div class="skill-card"><h4>{E(i.get("name",""))}</h4><div class="meta">{E(", ".join(i.get("categories",[])))}</div><p class="small muted" style="margin-bottom:4px">{E(i.get("description",""))}</p><div class="actions" style="margin-top:6px"><button class="sm ok" onclick="installSkill(\'{E(i.get("id",""))}\')">Install</button></div></div>' for i in list_skills_catalog())
    except Exception:
        sk_mkt = '<p class="muted">Marketplace unavailable</p>'

    def todo(ok, label, detail):
        ico = "\u2705" if ok else "\U0001f534"
        st = B("Supported", True) if ok else B("TODO", False)
        return f'<div class="todo-item"><div class="icon">{ico}</div><div class="body"><h4>{label}</h4><p>{detail}</p></div><div>{st}</div></div>'
    todo_html = "\n".join([
        todo(caps.get("supports_tools"),"Native Tool Calls","Upstream native tools/tool_calls passthrough"),
        todo(caps.get("supports_function_calls"),"Function Calls","OpenAI function_call"),
        todo(caps.get("supports_parallel_tool_calls"),"Parallel Tool Calls","Multiple tool calls per response"),
        todo(caps.get("supports_vision"),"Vision","Image input support"),
        todo(caps.get("supports_streaming"),"Streaming","SSE streaming"),
        todo(caps.get("supports_json_schema"),"JSON Schema","Structured output"),
        todo(caps.get("supports_network"),"Network","Web search"),
        todo(True,"Text Tool Call Fallback","Auto-inject tool descriptions when upstream lacks native support"),
        todo(True,"MCP Integration","External tool servers via MCP"),
        todo(True,"Builtin Tools",f"{unique_tool_count} built-in tools"),
        todo(True,"Skills System",f"{len(skills)} skills for LLM capability enhancement"),
    ])

    ctx = _client_config_snippets()
    model_alias = E(gateway_cfg.get("downstream_model_alias") or upstream.get("model",""))
    pub = E(public_base.rstrip("/"))
    claude_fn = ctx.get("claude_bash_profile_function","")
    claude_pretty = claude_fn.replace("\\n","\n") if "\\n" in claude_fn else claude_fn

    stats_pretty = _json_mod.dumps(stats, indent=2, ensure_ascii=False)
    config_pretty = _json_mod.dumps(redacted, indent=2, ensure_ascii=False)

    css = _ADMIN_CSS
    js = _ADMIN_JS

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gateway Control Center</title>
<style>{css}</style>
</head>
<body>
<div class="shell">
<div class="header">
  <div><h1>Gateway Control Center</h1><div class="subtitle">AI Tool FunctionCall Gateway</div></div>
  <div class="actions"><a href="/client-config" class="btn ghost">Client Config</a><a href="/healthz" class="btn ghost" target="_blank">Health</a></div>
</div>
<div class="stats-bar">
  <div class="stat"><div class="label">Model</div><div class="value">{E(upstream.get("model","—"))}</div></div>
  <div class="stat"><div class="label">Upstreams</div><div class="value">{len(upstream_profiles)}</div></div>
  <div class="stat"><div class="label">Keys</div><div class="value">{len(downstream_keys)}</div></div>
  <div class="stat"><div class="label">MCP</div><div class="value">{len(mcp_servers)}</div></div>
  <div class="stat"><div class="label">Tools</div><div class="value">{unique_tool_count}</div></div>
  <div class="stat"><div class="label">Skills</div><div class="value">{len(skills)}</div></div>
  <div class="stat"><div class="label">Requests</div><div class="value">{total_requests}</div></div>
  <div class="stat"><div class="label">Failures</div><div class="value" style="color:{'#dc2626' if failure_count else '#16a34a'}">{failure_count}</div></div>
</div>
<div class="tabs">
  <div class="tab active" data-tab="overview">1. 模型概览</div>
  <div class="tab" data-tab="models">2. 模型管理</div>
  <div class="tab" data-tab="usage">3. 接入指南</div>
  <div class="tab" data-tab="tools">4. Tools & MCP</div>
  <div class="tab" data-tab="skills">5. Skills</div>
  <div class="tab" data-tab="todo">6. 兼容性</div>
</div>

<div class="tab-content active" data-panel="overview">
<div class="grid2"><div class="card"><h2>当前活跃上游</h2><div class="info-table">
<div class="k">模型</div><div class="v"><b class="mono">{E(upstream.get("model",""))}</b></div>
<div class="k">Base URL</div><div class="v mono small">{E(upstream.get("base_url",""))}</div>
<div class="k">协议</div><div class="v">{E(upstream.get("protocol",""))}</div>
<div class="k">API Key</div><div class="v mono">{"••••"+E(str(upstream.get("api_key",""))[-4:]) if upstream.get("api_key") else "—"}</div>
<div class="k">超时</div><div class="v">{E(upstream.get("timeout_seconds",""))}s</div>
<div class="k">最大输入</div><div class="v">{E(upstream.get("max_input_tokens",""))}</div>
<div class="k">最大输出</div><div class="v">{E(upstream.get("max_output_tokens",""))}</div>
<div class="k">并发</div><div class="v">{E(upstream.get("max_concurrency",""))}</div>
<div class="k">Tools模式</div><div class="v">{E(upstream.get("tools_enabled",""))}</div>
</div></div>
<div class="card"><h2>能力声明</h2><div class="badge-wrap">{cap_badges}</div>
<h3>API 路径</h3><div class="info-table">
<div class="k">Chat</div><div class="v mono small">{E(paths.get("chat_completions",""))}</div>
<div class="k">Models</div><div class="v mono small">{E(paths.get("models",""))}</div>
<div class="k">Responses</div><div class="v mono small">{E(paths.get("responses",""))}</div>
<div class="k">Messages</div><div class="v mono small">{E(paths.get("messages",""))}</div>
</div></div></div>
<div class="grid2"><div class="card"><h2>Gateway 配置</h2><div class="info-table">
<div class="k">公开地址</div><div class="v mono small">{pub}</div>
<div class="k">模型别名</div><div class="v mono">{E(gateway_cfg.get("downstream_model_alias",""))}</div>
<div class="k">审查模型</div><div class="v mono">{E(gateway_cfg.get("review_model_alias",""))}</div>
<div class="k">上下文窗口</div><div class="v">{E(gateway_cfg.get("client_context_window",""))}</div>
<div class="k">工具模式</div><div class="v">{E(gateway_cfg.get("tool_mode",""))}</div>
<div class="k">最大轮数</div><div class="v">{E(gateway_cfg.get("max_tool_rounds",""))}</div>
</div></div>
<div class="card"><h2>上下文 & 记忆</h2><div class="info-table">
<div class="k">上下文管理</div><div class="v">{B("on" if context_cfg.get("enabled") else "off", context_cfg.get("enabled"))}</div>
<div class="k">记忆系统</div><div class="v">{B("on" if context_cfg.get("memory_enabled") else "off", context_cfg.get("memory_enabled"))}</div>
<div class="k">扇出</div><div class="v">{B("on" if context_cfg.get("fanout_enabled") else "off", context_cfg.get("fanout_enabled"))}</div>
<div class="k">质量审查</div><div class="v">{B("on" if context_cfg.get("quality_review_enabled") else "off", context_cfg.get("quality_review_enabled"))}</div>
<div class="k">最大输入</div><div class="v">{E(context_cfg.get("max_input_tokens",""))}</div>
<div class="k">摘要上限</div><div class="v">{E(context_cfg.get("summary_max_chars",""))} chars</div>
</div></div></div>
<div class="card"><h2>上游模型列表 ({len(upstream_profiles)})</h2><table><thead><tr><th>状态</th><th>名称</th><th>模型</th><th>协议</th><th>能力</th><th>操作</th></tr></thead><tbody>{profile_html}</tbody></table></div>
</div>

<div class="tab-content" data-panel="models">
<div class="grid2"><div class="card"><h2>编辑当前上游</h2><form method="POST" action="/admin/config" class="form-row">
<label class="field"><span>Base URL</span><input name="base_url" value="{E(upstream.get("base_url",""))}"></label>
<label class="field"><span>API Key</span><input name="api_key" type="password" value="{E(upstream.get("api_key",""))}"></label>
<label class="field"><span>模型</span><input name="model" value="{E(upstream.get("model",""))}"></label>
<label class="field"><span>协议</span><select name="protocol"><option value="openai_chat"{S(upstream.get("protocol"),"openai_chat")}>OpenAI Chat</option><option value="openai_responses"{S(upstream.get("protocol"),"openai_responses")}>OpenAI Responses</option><option value="anthropic_messages"{S(upstream.get("protocol"),"anthropic_messages")}>Anthropic</option></select></label>
<label class="field"><span>超时(秒)</span><input name="timeout" type="number" value="{E(upstream.get("timeout_seconds",60))}"></label>
<label class="field"><span>最大输入</span><input name="max_input_tokens" type="number" value="{E(upstream.get("max_input_tokens",1048576))}"></label>
<label class="field"><span>最大输出</span><input name="max_output_tokens" type="number" value="{E(upstream.get("max_output_tokens",131072))}"></label>
<label class="field"><span>并发</span><input name="max_concurrency" type="number" value="{E(upstream.get("max_concurrency",32))}"></label>
<label class="field"><span>Tools</span><select name="tools_enabled"><option value="auto"{S(upstream.get("tools_enabled"),"auto")}>Auto</option><option value="adapter"{S(upstream.get("tools_enabled"),"adapter")}>Adapter</option><option value="native"{S(upstream.get("tools_enabled"),"native")}>Native</option><option value="off"{S(upstream.get("tools_enabled"),"off")}>Off</option></select></label>
<div class="field full"><h3>能力</h3><div class="check-grid">{cap_inputs}</div></div>
<div class="field full"><button type="submit">保存配置</button></div>
</form></div>
<div class="card"><h2>新增上游模型</h2><form method="POST" action="/admin/upstream-profile" class="form-row">
<input type="hidden" name="action" value="add">
<label class="field"><span>名称</span><input name="name" required placeholder="GPT-4o"></label>
<label class="field"><span>Base URL</span><input name="base_url" id="base_url" required placeholder="https://api.openai.com/v1"></label>
<label class="field"><span>API Key</span><input name="api_key" id="api_key" type="password" placeholder="sk-..."></label>
<label class="field"><span>模型</span><input name="model" id="model" required list="upstream-model-options" placeholder="gpt-4o"><datalist id="upstream-model-options"></datalist></label>
<label class="field"><span>协议</span><select name="protocol" id="protocol"><option value="openai-chat">OpenAI Chat</option><option value="openai-responses">OpenAI Responses</option><option value="anthropic">Anthropic</option></select></label>
<label class="field"><span>Tools</span><select name="tools_enabled"><option value="auto">Auto</option><option value="native">Native</option><option value="prompt">Prompt</option><option value="off">Off</option></select></label>
<label class="field"><span>上下文</span><input name="context_window" type="number" value="128000"></label>
<label class="field"><span>最大输出</span><input name="max_tokens" type="number" value="16384"></label>
<label class="field wide"><span>Models Path</span><input name="path_models" id="path_models" placeholder="/v1/models"></label>
<div class="field"><button type="button" class="ghost" onclick="fetchModels()">Fetch Models</button> <span id="model-fetch-status" class="muted small"></span></div>
<div class="field full"><h3>能力</h3><div class="check-grid">{cap_inputs}</div></div>
<div class="field full"><button type="submit">添加上游</button></div>
</form></div></div>
<div class="grid2"><div class="card"><h2>下游 API Keys</h2><table><thead><tr><th>名称</th><th>前缀</th><th>状态</th><th>操作</th></tr></thead><tbody>{key_html}</tbody></table>
<h3>添加 Key</h3><form method="POST" action="/admin/downstream-key" class="form-row"><input type="hidden" name="action" value="add"><label class="field"><span>名称</span><input name="name" required></label><label class="field"><span>Key</span><input name="key" required></label><div class="field"><button type="submit">Add</button></div></form></div>
<div class="card"><h2>网关设置</h2><form method="POST" action="/admin/config" class="form-row">
<label class="field"><span>工具模式</span><select name="tool_mode"><option value="orchestrate"{S(gateway_cfg.get("tool_mode"),"orchestrate")}>Orchestrate</option><option value="passthrough"{S(gateway_cfg.get("tool_mode"),"passthrough")}>Passthrough</option></select></label>
<label class="field"><span>工作目录 (运行时)</span><input name="workspace_root_display" value="{E(gateway_cfg.get("workspace_root",""))}" readonly title="此字段从客户端请求动态提取，不可编辑" style="background:#f5f5f5;cursor:not-allowed;"></label>
<label class="field"><span>最大轮数</span><input name="max_tool_rounds" type="number" value="{E(gateway_cfg.get("max_tool_rounds",10))}"></label>
<label class="field"><span>并发</span><input name="max_concurrent_requests" type="number" value="{E(gateway_cfg.get("max_concurrent_requests",32))}"></label>
<label class="field"><span>超时</span><input name="tool_execution_timeout_seconds" type="number" value="{E(gateway_cfg.get("tool_execution_timeout_seconds",60))}"></label>
<div class="field full">
<label class="check-card"><input type="checkbox" name="allow_write_tools"{" checked" if gateway_cfg.get("allow_write_tools") else ""}><span><b>文件写入</b></span></label>
<label class="check-card"><input type="checkbox" name="allow_shell_tools"{" checked" if gateway_cfg.get("allow_shell_tools") else ""}><span><b>Shell</b></span></label>
<label class="check-card"><input type="checkbox" name="request_logging"{" checked" if gateway_cfg.get("request_logging",True) else ""}><span><b>日志</b></span></label>
<label class="check-card"><input type="checkbox" name="text_tool_call_fallback_enabled"{" checked" if gateway_cfg.get("text_tool_call_fallback_enabled",True) else ""}><span><b>文本工具回退</b></span></label>
</div>
<div class="field full"><button type="submit">保存网关设置</button></div>
</form></div></div>
</div>

<div class="tab-content" data-panel="usage">
<div class="card"><h2>OpenAI Compatible</h2><div class="grid2"><div><h3>Python SDK</h3><pre><code>from openai import OpenAI
client = OpenAI(base_url="{pub}/v1", api_key="KEY")
r = client.chat.completions.create(
    model="{model_alias}",
    messages=[{{"role":"user","content":"Hello"}}]
)</code></pre></div><div><h3>curl</h3><pre><code>curl {pub}/v1/chat/completions \\
  -H "Authorization: Bearer $KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"model":"{model_alias}","messages":[{{"role":"user","content":"Hi"}}]}}'</code></pre>
<h3>Anthropic</h3><pre><code>curl {pub}/anthropic/v1/messages \\
  -H "x-api-key: $KEY" -H "content-type: application/json" \\
  -d '{{"model":"{model_alias}","max_tokens":1024,"messages":[{{"role":"user","content":"Hi"}}]}}'</code></pre></div></div></div>
<div class="grid2"><div class="card"><h2>Codex CLI</h2><p class="muted small" style="margin-bottom:6px">~/.codex/config.toml</p><pre><code>{E(ctx.get("codex_config_toml",""))}</code></pre><h3>Auth JSON</h3><pre><code>{E(ctx.get("codex_auth_json",""))}</code></pre></div>
<div class="card"><h2>Claude Code</h2><p class="muted small" style="margin-bottom:6px">Shell profile</p><pre><code>{E(claude_pretty)}</code></pre></div></div>
<div class="grid2"><div class="card"><h2>OpenCode</h2><p class="muted small" style="margin-bottom:6px">opencode.json</p><pre><code>{E(ctx.get("opencode_json",""))}</code></pre></div>
<div class="card"><h2>VS Code + Claude</h2><p class="muted small" style="margin-bottom:6px">settings.json</p><pre><code>{E(ctx.get("vscode_claude_settings_json",""))}</code></pre></div></div>
</div>

<div class="tab-content" data-panel="tools">
<div class="card"><h2>MCP Servers ({len(mcp_servers)})</h2><table><thead><tr><th>名称</th><th>状态</th><th>Tools</th><th>操作</th></tr></thead><tbody>{mcp_html}</tbody></table>
<h3>添加 MCP Server</h3><form method="POST" action="/admin/mcp" class="form-row"><input type="hidden" name="action" value="add"><label class="field"><span>名称</span><input name="name" required></label><label class="field wide"><span>命令</span><input name="command" required placeholder="npx -y @modelcontextprotocol/server-filesystem /path"></label><div class="field"><button type="submit">Add</button></div></form>
<div style="margin-top:6px"><form method="POST" action="/admin/mcp-reload"><button class="ghost">Reload All MCP</button></form></div></div>
<div class="grid2"><div class="card"><h2>MCP Marketplace</h2><div class="skill-grid">{mcp_mkt}</div></div>
<div class="card"><h2>HTTP Actions</h2><table><thead><tr><th>名称</th><th>Method</th><th>URL</th><th>描述</th><th>状态</th></tr></thead><tbody>{action_html}</tbody></table>
<h3>添加 Action</h3><form method="POST" action="/admin/http-actions" class="form-row"><input type="hidden" name="action" value="add"><label class="field"><span>名称</span><input name="name" required></label><label class="field"><span>Method</span><select name="method"><option>POST</option><option>GET</option><option>PUT</option><option>DELETE</option></select></label><label class="field wide"><span>URL</span><input name="url" required></label><div class="field"><button type="submit">Add</button></div></form></div></div>
<div class="card"><h2>Builtin Tools ({unique_tool_count} unique / {tool_name_count} total)</h2><div class="search-bar"><input type="text" id="tool-search" placeholder="Search tools..."></div><div style="max-height:400px;overflow-y:auto"><table><thead><tr><th>名称</th><th>Canonical</th><th>Risk</th><th>描述</th></tr></thead><tbody id="tool-tbody">{tool_html}</tbody></table></div></div>
</div>

<div class="tab-content" data-panel="skills">
<div class="grid2"><div class="card"><h2>已安装 Skills ({len(skills)})</h2><div class="search-bar"><input type="text" id="skill-search" placeholder="Search skills..."></div><div class="skill-grid" id="skill-list">{skill_html}</div></div>
<div class="card"><h2>Skills Marketplace</h2><p class="muted small" style="margin-bottom:8px">Install from marketplace</p><div class="skill-grid">{sk_mkt}</div></div></div>
<div class="card"><h2>创建新 Skill</h2><form method="POST" action="/admin/skill-create" class="form-row">
<label class="field"><span>名称</span><input name="skill_name" required placeholder="my-skill"></label>
<label class="field wide"><span>描述</span><input name="skill_description" placeholder="What does this do?"></label>
<label class="field full"><span>SKILL.md</span><textarea name="skill_content" rows="10" placeholder="# Skill Name&#10;&#10;Description..."></textarea></label>
<div class="field full"><button type="submit" class="ok">创建 Skill</button></div>
</form></div>
</div>

<div class="tab-content" data-panel="todo">
<div class="card"><h2>Agent Runtime</h2><p class="muted small" style="margin-bottom:10px">统一状态 API：<code>/admin/agent-runtime.json?tenant_contains=&amp;workspace_contains=&amp;session_contains=</code></p><p class="muted small" style="margin-bottom:10px">事件时间线：<code>/admin/agent-runtime-events.json?event_type=planner_state</code></p><p class="muted small" style="margin-bottom:10px">需求审计：<code>/admin/agent-runtime-audit.json?tenant_contains=&amp;workspace_contains=&amp;session_contains=</code></p><p class="small">聚合 Agent Planner sessions、workflow、evidence、无限上下文 memory/rollup，以及 planner/memory/Gateway-owned/fallback dispatch 进展事件。</p></div>
<div class="card"><h2>Agent Runtime Events</h2><p class="muted small">最近 runtime timeline；按 tenant/workspace/session 归属，便于远端多用户排障。</p><table><thead><tr><th>时间</th><th>事件</th><th>Workflow</th><th>Step</th><th>Tenant</th><th>Workspace</th><th>摘要</th></tr></thead><tbody>{events_html}</tbody></table></div>
<div class="card"><h2>Tool Call 兼容性</h2><p class="muted small" style="margin-bottom:10px">当前上游能力状态</p>{todo_html}</div>
<div class="grid2"><div class="card"><h2>最近请求</h2><table><thead><tr><th>时间</th><th>路径</th><th>状态</th></tr></thead><tbody>{req_html}</tbody></table></div>
<div class="card"><h2>最近失败</h2><table><thead><tr><th>时间</th><th>工具</th><th>类型</th><th>内容</th></tr></thead><tbody>{fail_html}</tbody></table></div></div>
<div class="grid2"><div class="card"><h2>对话记忆</h2><p class="muted small">Scope-aware memory/rollup; API: <code>/admin/memories.json?tenant_contains=&amp;workspace_contains=&amp;session_contains=&amp;kind=session_rollup</code></p><table><thead><tr><th>时间</th><th>Tenant</th><th>Workspace</th><th>Session</th><th>类型</th><th>摘要</th></tr></thead><tbody>{mem_html}</tbody></table></div>
<div class="card"><h2>统计</h2><pre><code>{E(stats_pretty)}</code></pre></div></div>
<div class="card"><h2>配置 (Redacted)</h2><pre><code>{E(config_pretty)}</code></pre></div>
</div>

</div>
<div class="modal-overlay" id="skill-modal"><div class="modal"><button class="modal-close" onclick="closeSkillModal()">&times;</button><h2 id="skill-modal-title">Skill</h2><p class="muted small" id="skill-modal-path"></p><pre><code id="skill-modal-content">Loading...</code></pre></div></div>
<script>{js}</script>
</body></html>"""
