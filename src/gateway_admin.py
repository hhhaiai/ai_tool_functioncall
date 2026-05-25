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

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gateway Control Center</title>
<style>
:root {{ color-scheme: dark; --bg:#070b16; --panel:rgba(15,23,42,.78); --panel2:rgba(30,41,59,.72); --line:rgba(148,163,184,.22); --text:#e5eefb; --muted:#93a4bb; --brand:#7c3aed; --cyan:#22d3ee; --green:#34d399; --amber:#f59e0b; --red:#fb7185; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--text); background:radial-gradient(circle at 15% -10%, rgba(124,58,237,.45), transparent 36%), radial-gradient(circle at 92% 5%, rgba(34,211,238,.26), transparent 32%), linear-gradient(135deg,#050816 0%,#0f172a 48%,#111827 100%); }}
a {{ color:#a5f3fc; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.shell {{ max-width:1480px; margin:0 auto; padding:28px; }}
.hero {{ display:flex; align-items:flex-start; justify-content:space-between; gap:24px; padding:26px; border:1px solid var(--line); border-radius:28px; background:linear-gradient(135deg,rgba(15,23,42,.86),rgba(30,41,59,.6)); box-shadow:0 24px 80px rgba(0,0,0,.32); }}
.eyebrow {{ letter-spacing:.16em; text-transform:uppercase; color:#67e8f9; font-weight:800; font-size:12px; }}
h1 {{ margin:8px 0 8px; font-size:40px; line-height:1.05; }}
h2 {{ margin:0 0 16px; font-size:20px; }} h3 {{ margin:18px 0 10px; color:#dbeafe; }}
.muted {{ color:var(--muted); }} .mono {{ font-family:"SF Mono", Menlo, Consolas, monospace; }}
.nav {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; }} .nav a, .ghost {{ border:1px solid var(--line); border-radius:999px; padding:9px 13px; background:rgba(15,23,42,.5); color:#e0f2fe; }}
.grid {{ display:grid; gap:16px; }} .cards {{ grid-template-columns:repeat(4,minmax(0,1fr)); margin:18px 0; }}
.card, .section {{ border:1px solid var(--line); border-radius:22px; background:var(--panel); backdrop-filter:blur(14px); box-shadow:0 20px 60px rgba(0,0,0,.20); }} .card {{ padding:18px; }} .card .label {{ color:var(--muted); font-size:13px; }} .card .value {{ font-size:25px; font-weight:850; margin-top:5px; }}
.section {{ padding:22px; margin:18px 0; overflow:hidden; }} .two-col {{ display:grid; grid-template-columns:1.1fr .9fr; gap:18px; align-items:start; }}
.form-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }} .form-grid .wide {{ grid-column:span 2; }} .form-grid .full {{ grid-column:1/-1; }}
label.field {{ display:flex; flex-direction:column; gap:6px; color:#cbd5e1; font-size:13px; font-weight:700; }}
input, select, textarea {{ width:100%; border:1px solid rgba(148,163,184,.28); border-radius:14px; padding:11px 12px; background:rgba(2,6,23,.72); color:var(--text); outline:none; }} textarea {{ min-height:120px; resize:vertical; }}
input:focus, select:focus, textarea:focus {{ border-color:#67e8f9; box-shadow:0 0 0 3px rgba(34,211,238,.15); }}
button {{ border:0; border-radius:14px; padding:10px 14px; color:white; background:linear-gradient(135deg,var(--brand),#2563eb); font-weight:800; cursor:pointer; box-shadow:0 10px 28px rgba(37,99,235,.24); }} button:hover {{ transform:translateY(-1px); }} button.danger {{ background:linear-gradient(135deg,#e11d48,#dc2626); }} button.secondary {{ background:rgba(30,41,59,.92); border:1px solid var(--line); }}
.actions {{ display:flex; gap:8px; flex-wrap:wrap; }} form.inline, td form {{ display:inline-flex; margin:0 6px 6px 0; }}
.table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:16px; }} table {{ width:100%; border-collapse:collapse; min-width:760px; }} th,td {{ padding:11px 12px; border-bottom:1px solid rgba(148,163,184,.14); text-align:left; vertical-align:top; }} th {{ color:#bfdbfe; background:rgba(15,23,42,.72); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }} tr.active-row {{ background:rgba(34,197,94,.08); }}
.badge, .pill {{ display:inline-flex; align-items:center; gap:5px; border-radius:999px; padding:4px 9px; font-size:12px; font-weight:800; margin:2px 3px 2px 0; border:1px solid transparent; }} .badge-ok,.pill.active {{ color:#bbf7d0; background:rgba(34,197,94,.16); border-color:rgba(52,211,153,.25); }} .badge-muted,.pill {{ color:#cbd5e1; background:rgba(100,116,139,.16); border-color:rgba(148,163,184,.18); }} .badge-warn {{ color:#fde68a; background:rgba(245,158,11,.16); border-color:rgba(245,158,11,.25); }}
.check-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }} .check-card {{ display:flex; gap:10px; align-items:flex-start; padding:12px; border:1px solid var(--line); border-radius:16px; background:rgba(15,23,42,.55); cursor:pointer; }} .check-card input {{ width:auto; margin-top:3px; }} .check-card small {{ display:block; color:var(--muted); font-weight:500; margin-top:3px; }}
pre {{ margin:0; max-height:420px; overflow:auto; border:1px solid var(--line); border-radius:16px; padding:14px; background:#020617; color:#dbeafe; font-size:12px; }} .status-line {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }}
@media (max-width:1100px) {{ .cards,.form-grid,.check-grid,.two-col {{ grid-template-columns:1fr; }} .form-grid .wide {{ grid-column:auto; }} .shell {{ padding:16px; }} h1 {{ font-size:31px; }} .hero {{ flex-direction:column; }} }}
</style>
</head>
<body>
<div class="shell">
  <header class="hero"><div><div class="eyebrow">Native Tool Gateway</div><h1>Gateway Control Center</h1><p class="muted">统一管理上游 API、Claude Code/Codex 客户端配置、工具能力、MCP、HTTP Actions 与运行日志。</p><div class="nav"><a href="/client-config">Client Configuration</a><a href="/admin/config.json">Config JSON</a><a href="/admin/stats.json">Stats JSON</a><a href="/healthz">Healthz</a></div></div><div class="card" style="min-width:320px"><div class="label">Active upstream</div><div class="value">{esc(upstream.get('name') or active_upstream_id or 'default')}</div><div class="muted mono">{esc(upstream.get('base_url'))}</div><div class="status-line">{capability_badges}</div></div></header>
  <section class="grid cards"><div class="card"><div class="label">Model</div><div class="value mono">{esc(active_model or 'not set')}</div></div><div class="card"><div class="label">Tools exposed</div><div class="value">{unique_tool_count}</div><div class="muted">{tool_name_count} aliases</div></div><div class="card"><div class="label">Requests</div><div class="value">{esc(total_requests)}</div><div class="muted">SQLite/JSON stats</div></div><div class="card"><div class="label">Recent failures</div><div class="value">{failure_count}</div><div class="muted">last 20 rows</div></div></section>
  <section class="section" id="upstream"><h2>上游模型与 API 设置</h2><p class="muted">这里是稳定运行的关键入口：配置上游地址、模型、协议路径，并明确上游是否支持 tools / function call / 识图。点击按钮会通过 <span class="mono">/v1/models</span> 拉取模型列表。</p><form method="POST" action="/admin/config" id="upstream-form"><input type="hidden" name="capabilities_form" value="1"><div class="form-grid"><label class="field"><span>Profile ID</span><input name="profile_id" value="{esc(upstream.get('id') or active_upstream_id or 'default')}" placeholder="default"></label><label class="field"><span>Name</span><input name="profile_name" value="{esc(upstream.get('name') or 'default')}" placeholder="Mimo upstream"></label><label class="field wide"><span>Base URL</span><input id="base_url" name="base_url" value="{esc(upstream.get('base_url'))}" placeholder="http://upstream.example.local:8885" required></label><label class="field"><span>API Key</span><input id="api_key" name="api_key" type="password" placeholder="留空则保留现有 key"></label><label class="field"><span>Model</span><input id="model" name="model" value="{esc(upstream.get('model'))}" list="upstream-model-options" placeholder="mimo-v2.5-pro"></label><datalist id="upstream-model-options"></datalist><label class="field"><span>Protocol</span><select id="protocol" name="protocol">{option_rows([('OpenAI Chat','openai_chat'),('Anthropic Messages','anthropic_messages'),('OpenAI Responses','openai_responses')], upstream.get('protocol'))}</select></label><label class="field"><span>Tools mode</span><select name="tools_enabled">{option_rows([('Auto: 按能力自动选择','auto'),('Native: 发送原生 tools','native'),('Native only: 不支持即失败','native_only'),('Adapter: 本地真实工具适配','adapter'),('Text only: 文本工具标签','text_only'),('Off/Adapter compatible','off')], upstream.get('tools_enabled'))}</select></label><div class="field"><span>Model discovery</span><button class="secondary" type="button" id="fetch-models">Fetch Models /v1/models</button><small class="muted" id="model-fetch-status">未拉取</small></div></div><h3>Capability Matrix / 能力矩阵</h3><div class="check-grid">{capability_inputs}<label class="check-card"><input type="checkbox" name="native_tools_verified" value="1"{checked(upstream.get('native_tools_verified'))}><span><b>Native verified</b><small>已通过强制 tool probe</small></span></label><label class="check-card"><input type="checkbox" name="use_for_coding" value="1"{checked(upstream.get('use_for_coding', True))}><span><b>Coding traffic</b><small>允许 Claude Code/Codex 使用</small></span></label></div><h3>Upstream Paths</h3><div class="form-grid"><label class="field"><span>Models path</span><input id="path_models" name="path_models" value="{esc(paths.get('models') or '/v1/models')}"></label><label class="field"><span>Chat Completions</span><input name="path_chat_completions" value="{esc(paths.get('chat_completions') or '/v1/chat/completions')}"></label><label class="field"><span>Responses</span><input name="path_responses" value="{esc(paths.get('responses') or '/v1/responses')}"></label><label class="field"><span>Messages</span><input name="path_messages" value="{esc(paths.get('messages') or '/v1/messages')}"></label></div><h3>Runtime Limits</h3><div class="form-grid"><label class="field"><span>Upstream timeout seconds</span><input name="upstream_timeout_seconds" value="{esc(upstream.get('timeout_seconds', 60.0))}"></label><label class="field"><span>Max input tokens</span><input name="upstream_max_input_tokens" value="{esc(upstream.get('max_input_tokens', 1048576))}"></label><label class="field"><span>Max output tokens</span><input name="upstream_max_output_tokens" value="{esc(upstream.get('max_output_tokens', 131072))}"></label><label class="field"><span>Max concurrency</span><input name="upstream_max_concurrency" value="{esc(upstream.get('max_concurrency', 32))}"></label><label class="field"><span>Gateway tool mode</span><select name="tool_mode">{option_rows([('Orchestrate','orchestrate'),('Native passthrough','native_passthrough'),('Proxy','proxy')], gateway_cfg.get('tool_mode'))}</select></label><label class="field"><span>Max tool rounds</span><input name="max_tool_rounds" value="{esc(gateway_cfg.get('max_tool_rounds', 5))}"></label><label class="field"><span>Max concurrent requests</span><input name="max_concurrent_requests" value="{esc(gateway_cfg.get('max_concurrent_requests', 32))}"></label><label class="field"><span>Tool timeout seconds</span><input name="tool_execution_timeout_seconds" value="{esc(gateway_cfg.get('tool_execution_timeout_seconds', 60.0))}"></label><label class="field"><span>Text adapter compact tokens</span><input name="text_tool_adapter_compact_token_limit" value="{esc(gateway_cfg.get('text_tool_adapter_compact_token_limit', 48000))}"></label><label class="field"><span>Queue timeout seconds</span><input name="concurrency_queue_timeout_seconds" value="{esc(gateway_cfg.get('concurrency_queue_timeout_seconds', 5.0))}"></label><label class="field wide"><span>Workspace root</span><input name="workspace_root" value="{esc(gateway_cfg.get('workspace_root'))}"></label></div><h3>Gateway Safety / Context</h3><div class="check-grid"><label class="check-card"><input type="checkbox" name="allow_write_tools" value="1"{checked(gateway_cfg.get('allow_write_tools'))}><span><b>Allow write tools</b><small>允许写文件/编辑</small></span></label><label class="check-card"><input type="checkbox" name="allow_shell_tools" value="1"{checked(gateway_cfg.get('allow_shell_tools'))}><span><b>Allow shell tools</b><small>允许 Bash/exec</small></span></label><label class="check-card"><input type="checkbox" name="request_logging" value="1"{checked(gateway_cfg.get('request_logging', True))}><span><b>Request logging</b><small>记录请求用于审计</small></span></label><label class="check-card"><input type="checkbox" name="record_unsupported_tools" value="1"{checked(gateway_cfg.get('record_unsupported_tools', True))}><span><b>Unsupported tools log</b><small>记录不支持工具</small></span></label><label class="check-card"><input type="checkbox" name="text_tool_call_fallback_enabled" value="1"{checked(gateway_cfg.get('text_tool_call_fallback_enabled', True))}><span><b>Text fallback</b><small>弱模型文本工具调用</small></span></label><label class="check-card"><input type="checkbox" name="context_enabled" value="1"{checked(context_cfg.get('enabled', True))}><span><b>Context compact</b><small>上下文压缩</small></span></label><label class="check-card"><input type="checkbox" name="context_fanout_enabled" value="1"{checked(context_cfg.get('fanout_enabled', True))}><span><b>Fanout</b><small>超长上下文分片</small></span></label><label class="check-card"><input type="checkbox" name="context_quality_review_enabled" value="1"{checked(context_cfg.get('quality_review_enabled', True))}><span><b>Quality review</b><small>汇总质量复核</small></span></label></div><div class="form-grid" style="margin-top:12px"><label class="field"><span>Context max input</span><input name="context_max_input_tokens" value="{esc(context_cfg.get('max_input_tokens', 1048576))}"></label><label class="field"><span>Fanout chunk tokens</span><input name="context_fanout_chunk_tokens" value="{esc(context_cfg.get('fanout_chunk_tokens', 120000))}"></label><label class="field"><span>Fanout max chunks</span><input name="context_fanout_max_chunks" value="{esc(context_cfg.get('fanout_max_chunks', 0))}"></label><label class="field"><span>Fanout workers</span><input name="context_fanout_max_workers" value="{esc(context_cfg.get('fanout_max_workers', 4))}"></label></div><div class="actions" style="margin-top:18px"><button type="submit">Save upstream + gateway config</button><a class="ghost" href="/client-config">查看 Claude/Codex 启动配置</a></div></form></section>
  <section class="section"><h2>Upstream Profiles</h2><div class="table-wrap"><table><tr><th>Status</th><th>Name / Base URL</th><th>Model</th><th>Protocol</th><th>Capabilities</th><th>Actions</th></tr>{''.join(profile_rows) or '<tr><td colspan="6" class="muted">No profiles</td></tr>'}</table></div></section>
  <div class="two-col"><section class="section"><h2>Downstream Keys</h2><div class="table-wrap"><table><tr><th>Name</th><th>Prefix</th><th>Status</th><th>Actions</th></tr>{''.join(key_rows) or '<tr><td colspan="4" class="muted">No keys</td></tr>'}</table></div><h3>Add Key</h3><form method="POST" action="/admin/downstream-key" class="form-grid"><input type="hidden" name="action" value="add"><label class="field"><span>Name</span><input name="name" required></label><label class="field wide"><span>API Key</span><input name="key" required></label><div class="field"><span>&nbsp;</span><button type="submit">Add Key</button></div></form></section><section class="section"><h2>Client Endpoints</h2><pre><code>OpenAI:    {esc(public_base)}/v1
Anthropic: {esc(public_base)}/anthropic
Claude:    ANTHROPIC_BASE_URL={esc(public_base)}/anthropic
Key:       {esc(gateway_cfg.get('client_snippet_api_key') or '(set in client config)')}</code></pre><h3>Claude Code 函数</h3><p class="muted">复制到 shell profile 后执行 <span class="mono">claude_mnative -p "Reply with OK only."</span> 做 smoke。</p><pre><code>{claude_function_pretty}</code></pre></section></div>
  <section class="section"><h2>MCP Servers</h2><div class="table-wrap"><table><tr><th>Name</th><th>Status</th><th>Tools</th><th>Actions</th></tr>{''.join(mcp_rows) or '<tr><td colspan="4" class="muted">No MCP servers configured</td></tr>'}</table></div><h3>Add Server</h3><form method="POST" action="/admin/mcp" class="form-grid"><input type="hidden" name="action" value="add"><label class="field"><span>Name</span><input name="name" required></label><label class="field wide"><span>Command</span><input name="command" placeholder="npx -y @modelcontextprotocol/server-filesystem /path" required></label><div class="field"><span>&nbsp;</span><button type="submit">Add Server</button></div></form><form method="POST" action="/admin/mcp-reload" style="margin-top:10px"><button type="submit" class="secondary">Reload All</button></form></section>
  <section class="section"><h2>Builtin Tools ({unique_tool_count} unique / {tool_name_count} exposed names)</h2><div class="table-wrap"><table><tr><th>Name</th><th>Canonical</th><th>Risk</th><th>Description</th></tr>{''.join(tool_rows)}</table></div></section>
  <div class="two-col"><section class="section"><h2>Recent Requests</h2><div class="table-wrap"><table><tr><th>Time</th><th>Path</th><th>Status</th></tr>{''.join(request_rows) or '<tr><td colspan="3" class="muted">No requests yet</td></tr>'}</table></div></section><section class="section"><h2>Recent Failures</h2><div class="table-wrap"><table><tr><th>Time</th><th>Tool</th><th>Type</th><th>Content</th></tr>{''.join(failure_rows) or '<tr><td colspan="4" class="muted">No failures in tail</td></tr>'}</table></div></section></div>
  <section class="section"><h2>Conversation Memories</h2><div class="table-wrap"><table><tr><th>Time</th><th>Session</th><th>Kind</th><th>Summary</th></tr>{''.join(memory_rows) or '<tr><td colspan="4" class="muted">No memories</td></tr>'}</table></div></section>
  <div class="two-col"><section class="section"><h2>Statistics</h2><pre><code>{stats_pretty}</code></pre></section><section class="section"><h2>Redacted Config</h2><pre><code>{config_pretty}</code></pre></section></div>
</div>
<script>
(function() {{
  const button = document.getElementById('fetch-models');
  const status = document.getElementById('model-fetch-status');
  const model = document.getElementById('model');
  const datalist = document.getElementById('upstream-model-options');
  button && button.addEventListener('click', async function() {{
    status.textContent = 'fetching...';
    const params = new URLSearchParams();
    for (const id of ['base_url','api_key','protocol','path_models']) {{
      const el = document.getElementById(id);
      if (el && el.value) params.set(id, el.value);
    }}
    try {{
      const resp = await fetch('/admin/upstream-models.json', {{
        method: 'POST',
        headers: {{'accept':'application/json', 'content-type':'application/x-www-form-urlencoded'}},
        body: params.toString()
      }});
      const payload = await resp.json();
      if (!resp.ok || !payload.ok) throw new Error((payload.error && payload.error.message) || resp.statusText);
      datalist.innerHTML = '';
      for (const item of payload.models || []) {{
        const option = document.createElement('option'); option.value = item; datalist.appendChild(option);
      }}
      if ((!model.value) && payload.models && payload.models.length) model.value = payload.models[0];
      status.textContent = 'loaded ' + (payload.models || []).length + ' models from ' + payload.path;
    }} catch (err) {{
      status.textContent = 'failed: ' + err.message;
    }}
  }});
}})();
</script>
</body>
</html>'''
