"""Web configuration UI module for the gateway.

Provides a modern tab-based configuration interface for managing
gateway settings, upstream configurations, and system parameters.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Any

Json = dict[str, Any]


# ---------------------------------------------------------------------------
# Configuration Schema
# ---------------------------------------------------------------------------

@dataclass
class ConfigField:
    """Definition of a configuration field."""
    name: str
    label: str
    field_type: str  # text, number, boolean, select, textarea, password
    description: str = ""
    default: Any = None
    required: bool = False
    options: list[dict[str, str]] = field(default_factory=list)
    min_value: float | None = None
    max_value: float | None = None
    placeholder: str = ""


@dataclass
class ConfigTab:
    """Definition of a configuration tab."""
    id: str
    label: str
    icon: str
    description: str
    fields: list[ConfigField] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration Tabs Definition
# ---------------------------------------------------------------------------

def _get_config_tabs() -> list[ConfigTab]:
    """Get all configuration tabs."""
    return [
        ConfigTab(
            id="upstream",
            label="上游配置",
            icon="🔗",
            description="配置上游 API 服务器",
            fields=[
                ConfigField(
                    name="upstream.url",
                    label="上游 URL",
                    field_type="text",
                    description="上游 API 服务器地址",
                    required=True,
                    placeholder="https://api.example.com",
                ),
                ConfigField(
                    name="upstream.api_key",
                    label="API Key",
                    field_type="password",
                    description="上游 API 密钥",
                    required=True,
                ),
                ConfigField(
                    name="upstream.model",
                    label="模型",
                    field_type="text",
                    description="使用的模型名称",
                    placeholder="gpt-4",
                ),
                ConfigField(
                    name="upstream.timeout",
                    label="超时时间 (秒)",
                    field_type="number",
                    description="请求超时时间",
                    default=60,
                    min_value=1,
                    max_value=600,
                ),
            ],
        ),
        ConfigTab(
            id="capabilities",
            label="能力配置",
            icon="⚡",
            description="配置上游 API 能力",
            fields=[
                ConfigField(
                    name="upstream.capabilities.supports_tools",
                    label="支持 Tools",
                    field_type="boolean",
                    description="上游是否支持 tool_calls",
                    default=False,
                ),
                ConfigField(
                    name="upstream.capabilities.supports_function_calls",
                    label="支持 Function Calls",
                    field_type="boolean",
                    description="上游是否支持 function_call",
                    default=False,
                ),
                ConfigField(
                    name="upstream.capabilities.supports_streaming",
                    label="支持流式响应",
                    field_type="boolean",
                    description="上游是否支持 SSE 流式响应",
                    default=True,
                ),
                ConfigField(
                    name="upstream.capabilities.supports_vision",
                    label="支持视觉",
                    field_type="boolean",
                    description="上游是否支持图片输入",
                    default=False,
                ),
                ConfigField(
                    name="upstream.capabilities.supports_network",
                    label="支持网络",
                    field_type="boolean",
                    description="上游是否支持联网搜索",
                    default=False,
                ),
            ],
        ),
        ConfigTab(
            id="context",
            label="上下文配置",
            icon="📝",
            description="配置无限上下文和记忆系统",
            fields=[
                ConfigField(
                    name="context.enabled",
                    label="启用上下文管理",
                    field_type="boolean",
                    description="启用上下文压缩和记忆系统",
                    default=True,
                ),
                ConfigField(
                    name="context.max_input_tokens",
                    label="最大输入 Token",
                    field_type="number",
                    description="最大输入 token 数量",
                    default=1048576,
                    min_value=1000,
                    max_value=10000000,
                ),
                ConfigField(
                    name="context.keep_recent_messages",
                    label="保留最近消息数",
                    field_type="number",
                    description="压缩时保留的最近消息数量",
                    default=12,
                    min_value=1,
                    max_value=100,
                ),
                ConfigField(
                    name="context.summary_max_chars",
                    label="摘要最大字符数",
                    field_type="number",
                    description="单条消息摘要的最大字符数",
                    default=6000,
                    min_value=100,
                    max_value=50000,
                ),
                ConfigField(
                    name="context.fanout_enabled",
                    label="启用扇出并行",
                    field_type="boolean",
                    description="启用长对话扇出并行处理",
                    default=True,
                ),
                ConfigField(
                    name="context.fanout_chunk_tokens",
                    label="扇出分块 Token",
                    field_type="number",
                    description="扇出并行时每块的 token 数量",
                    default=120000,
                    min_value=10000,
                    max_value=500000,
                ),
                ConfigField(
                    name="context.memory_enabled",
                    label="启用记忆系统",
                    field_type="boolean",
                    description="启用对话记忆持久化",
                    default=True,
                ),
            ],
        ),
        ConfigTab(
            id="intelligence",
            label="智力提升",
            icon="🧠",
            description="配置智能增强功能",
            fields=[
                ConfigField(
                    name="intelligence.enabled",
                    label="启用智力提升",
                    field_type="boolean",
                    description="启用问题分析和回答质量评估",
                    default=True,
                ),
                ConfigField(
                    name="intelligence.reflection_enabled",
                    label="启用反思",
                    field_type="boolean",
                    description="启用复杂问题的反思机制",
                    default=True,
                ),
                ConfigField(
                    name="intelligence.decomposition_enabled",
                    label="启用问题分解",
                    field_type="boolean",
                    description="启用复杂问题的自动分解",
                    default=True,
                ),
                ConfigField(
                    name="intelligence.quality_assessment_enabled",
                    label="启用质量评估",
                    field_type="boolean",
                    description="启用回答质量自动评估",
                    default=True,
                ),
                ConfigField(
                    name="intelligence.quality_threshold",
                    label="质量阈值",
                    field_type="number",
                    description="回答质量阈值 (0-1)",
                    default=0.6,
                    min_value=0.0,
                    max_value=1.0,
                ),
            ],
        ),
        ConfigTab(
            id="concurrency",
            label="并发配置",
            icon="🚀",
            description="配置并发和负载均衡",
            fields=[
                ConfigField(
                    name="concurrency.enabled",
                    label="启用并发优化",
                    field_type="boolean",
                    description="启用连接池和负载均衡",
                    default=True,
                ),
                ConfigField(
                    name="concurrency.max_connections",
                    label="最大连接数",
                    field_type="number",
                    description="最大并发连接数",
                    default=100,
                    min_value=1,
                    max_value=1000,
                ),
                ConfigField(
                    name="concurrency.max_connections_per_host",
                    label="每主机最大连接数",
                    field_type="number",
                    description="每个上游主机的最大连接数",
                    default=10,
                    min_value=1,
                    max_value=100,
                ),
                ConfigField(
                    name="concurrency.retry_count",
                    label="重试次数",
                    field_type="number",
                    description="请求失败后的重试次数",
                    default=2,
                    min_value=0,
                    max_value=10,
                ),
                ConfigField(
                    name="concurrency.load_balance_strategy",
                    label="负载均衡策略",
                    field_type="select",
                    description="多上游时的负载均衡策略",
                    default="round_robin",
                    options=[
                        {"value": "round_robin", "label": "轮询 (Round Robin)"},
                        {"value": "least_connections", "label": "最少连接"},
                        {"value": "random", "label": "随机"},
                    ],
                ),
            ],
        ),
        ConfigTab(
            id="cache",
            label="缓存配置",
            icon="💾",
            description="配置智能缓存系统",
            fields=[
                ConfigField(
                    name="cache.semantic_enabled",
                    label="启用语义缓存",
                    field_type="boolean",
                    description="启用基于语义相似度的缓存",
                    default=True,
                ),
                ConfigField(
                    name="cache.semantic_ttl",
                    label="语义缓存 TTL (秒)",
                    field_type="number",
                    description="语义缓存条目过期时间",
                    default=3600,
                    min_value=60,
                    max_value=86400,
                ),
                ConfigField(
                    name="cache.semantic_threshold",
                    label="相似度阈值",
                    field_type="number",
                    description="语义相似度阈值 (0-1)",
                    default=0.85,
                    min_value=0.5,
                    max_value=1.0,
                ),
                ConfigField(
                    name="cache.tool_result_enabled",
                    label="启用工具结果缓存",
                    field_type="boolean",
                    description="缓存确定性工具的执行结果",
                    default=True,
                ),
                ConfigField(
                    name="cache.tool_result_ttl",
                    label="工具缓存 TTL (秒)",
                    field_type="number",
                    description="工具结果缓存过期时间",
                    default=300,
                    min_value=10,
                    max_value=3600,
                ),
            ],
        ),
        ConfigTab(
            id="tools",
            label="工具配置",
            icon="🔧",
            description="配置内置工具和 MCP",
            fields=[
                ConfigField(
                    name="tools.builtin_enabled",
                    label="启用内置工具",
                    field_type="boolean",
                    description="启用 Gateway 内置工具 (echo_probe, calculator 等)",
                    default=True,
                ),
                ConfigField(
                    name="tools.claude_compat_enabled",
                    label="启用 Claude 兼容",
                    field_type="boolean",
                    description="启用 Claude Code 工具兼容层",
                    default=True,
                ),
                ConfigField(
                    name="tools.mcp_enabled",
                    label="启用 MCP",
                    field_type="boolean",
                    description="启用 Model Context Protocol 支持",
                    default=True,
                ),
                ConfigField(
                    name="tools.http_actions_enabled",
                    label="启用 HTTP Actions",
                    field_type="boolean",
                    description="启用 HTTP 端点作为工具",
                    default=True,
                ),
            ],
        ),
        ConfigTab(
            id="web2api",
            label="Web2API",
            icon="🌐",
            description="配置网页转 API 功能",
            fields=[
                ConfigField(
                    name="web2api.enabled",
                    label="启用 Web2API",
                    field_type="boolean",
                    description="启用网页内容提取为 API",
                    default=True,
                ),
                ConfigField(
                    name="web2api.timeout",
                    label="请求超时 (秒)",
                    field_type="number",
                    description="网页请求超时时间",
                    default=30,
                    min_value=5,
                    max_value=120,
                ),
                ConfigField(
                    name="web2api.max_content_length",
                    label="最大内容长度",
                    field_type="number",
                    description="提取内容的最大字符数",
                    default=50000,
                    min_value=1000,
                    max_value=500000,
                ),
            ],
        ),
        ConfigTab(
            id="security",
            label="安全配置",
            icon="🔒",
            description="配置安全和认证",
            fields=[
                ConfigField(
                    name="gateway.auth_required",
                    label="启用认证",
                    field_type="boolean",
                    description="要求下游请求携带 API Key",
                    default=True,
                ),
                ConfigField(
                    name="gateway.rate_limit_enabled",
                    label="启用限流",
                    field_type="boolean",
                    description="启用请求速率限制",
                    default=True,
                ),
                ConfigField(
                    name="gateway.rate_limit_rpm",
                    label="每分钟请求数",
                    field_type="number",
                    description="每分钟最大请求数",
                    default=60,
                    min_value=1,
                    max_value=10000,
                ),
                ConfigField(
                    name="gateway.cors_enabled",
                    label="启用 CORS",
                    field_type="boolean",
                    description="启用跨域资源共享",
                    default=False,
                ),
                ConfigField(
                    name="gateway.cors_allowed_origins",
                    label="CORS Origin 白名单",
                    field_type="text",
                    description="逗号分隔的精确 http(s) Origin；不支持 * 通配符",
                    default="",
                    placeholder="https://console.example.com",
                ),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# HTML Rendering
# ---------------------------------------------------------------------------

def _render_field(field: ConfigField, current_value: Any = None) -> str:
    """Render a single configuration field."""
    name = html.escape(field.name)
    label = html.escape(field.label)
    desc = html.escape(field.description)
    placeholder = html.escape(field.placeholder)
    value = current_value if current_value is not None else field.default

    required_attr = " required" if field.required else ""
    required_mark = " *" if field.required else ""

    if field.field_type == "boolean":
        checked = " checked" if value else ""
        return f"""
        <div class="config-field">
            <label class="toggle-label">
                <input type="checkbox" name="{name}"{checked}{required_attr}>
                <span class="toggle-slider"></span>
                <span class="toggle-text">{label}{required_mark}</span>
            </label>
            <p class="field-desc">{desc}</p>
        </div>"""

    elif field.field_type == "select":
        options_html = ""
        for opt in field.options:
            opt_value = html.escape(opt.get("value", ""))
            opt_label = html.escape(opt.get("label", ""))
            selected = " selected" if str(value) == opt_value else ""
            options_html += f'<option value="{opt_value}"{selected}>{opt_label}</option>'

        return f"""
        <div class="config-field">
            <label for="{name}">{label}{required_mark}</label>
            <select name="{name}" id="{name}"{required_attr}>
                {options_html}
            </select>
            <p class="field-desc">{desc}</p>
        </div>"""

    elif field.field_type == "textarea":
        return f"""
        <div class="config-field">
            <label for="{name}">{label}{required_mark}</label>
            <textarea name="{name}" id="{name}" placeholder="{placeholder}" rows="4"{required_attr}>{html.escape(str(value or ''))}</textarea>
            <p class="field-desc">{desc}</p>
        </div>"""

    elif field.field_type == "password":
        return f"""
        <div class="config-field">
            <label for="{name}">{label}{required_mark}</label>
            <input type="password" name="{name}" id="{name}" value="{html.escape(str(value or ''))}" placeholder="{placeholder}"{required_attr}>
            <p class="field-desc">{desc}</p>
        </div>"""

    elif field.field_type == "number":
        min_attr = f' min="{field.min_value}"' if field.min_value is not None else ""
        max_attr = f' max="{field.max_value}"' if field.max_value is not None else ""
        return f"""
        <div class="config-field">
            <label for="{name}">{label}{required_mark}</label>
            <input type="number" name="{name}" id="{name}" value="{html.escape(str(value or ''))}"{min_attr}{max_attr}{required_attr}>
            <p class="field-desc">{desc}</p>
        </div>"""

    else:  # text
        return f"""
        <div class="config-field">
            <label for="{name}">{label}{required_mark}</label>
            <input type="text" name="{name}" id="{name}" value="{html.escape(str(value or ''))}" placeholder="{placeholder}"{required_attr}>
            <p class="field-desc">{desc}</p>
        </div>"""


def _render_tab(tab: ConfigTab, config: dict[str, Any]) -> str:
    """Render a single configuration tab."""
    tab_id = html.escape(tab.id)
    icon = tab.icon
    label = html.escape(tab.label)
    desc = html.escape(tab.description)

    fields_html = ""
    for field in tab.fields:
        # Get current value from config
        value = _get_nested_value(config, field.name)
        fields_html += _render_field(field, value)

    return f"""
    <div class="tab-panel" id="tab-{tab_id}">
        <div class="tab-header">
            <h2>{icon} {label}</h2>
            <p>{desc}</p>
        </div>
        <div class="tab-content">
            {fields_html}
        </div>
    </div>"""


def _get_nested_value(config: dict[str, Any], path: str) -> Any:
    """Get a nested value from config using dot notation."""
    keys = path.split(".")
    current = config
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _set_nested_value(config: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Set a nested value in config using dot notation."""
    keys = path.split(".")
    current = config
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
    return config


# ---------------------------------------------------------------------------
# Main UI Renderer
# ---------------------------------------------------------------------------

def render_web_config_ui(config: dict[str, Any] | None = None) -> str:
    """Render the complete web configuration UI."""
    if config is None:
        config = {}

    tabs = _get_config_tabs()

    # Render tab navigation
    tab_nav = ""
    for i, tab in enumerate(tabs):
        active = " active" if i == 0 else ""
        tab_nav += f"""
        <button class="tab-btn{active}" data-tab="tab-{html.escape(tab.id)}">
            <span class="tab-icon">{tab.icon}</span>
            <span class="tab-label">{html.escape(tab.label)}</span>
        </button>"""

    # Render tab panels
    tab_panels = ""
    for tab in tabs:
        tab_panels += _render_tab(tab, config)

    # Build complete page
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gateway 配置</title>
    <style>
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }}
        header {{
            background: #fff;
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        header h1 {{
            font-size: 24px;
            color: #1a1a1a;
        }}
        header p {{
            color: #666;
            margin-top: 5px;
        }}
        .tabs-container {{
            background: #fff;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .tabs-nav {{
            display: flex;
            flex-wrap: wrap;
            background: #fafafa;
            border-bottom: 1px solid #e0e0e0;
            padding: 10px 10px 0;
        }}
        .tab-btn {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 12px 20px;
            border: none;
            background: transparent;
            cursor: pointer;
            font-size: 14px;
            color: #666;
            border-bottom: 3px solid transparent;
            transition: all 0.2s;
            margin-bottom: -1px;
        }}
        .tab-btn:hover {{
            color: #333;
            background: #f0f0f0;
        }}
        .tab-btn.active {{
            color: #2563eb;
            border-bottom-color: #2563eb;
            background: #fff;
        }}
        .tab-icon {{
            font-size: 18px;
        }}
        .tab-panel {{
            display: none;
            padding: 30px;
        }}
        .tab-panel.active {{
            display: block;
        }}
        .tab-header {{
            margin-bottom: 25px;
        }}
        .tab-header h2 {{
            font-size: 20px;
            color: #1a1a1a;
            margin-bottom: 5px;
        }}
        .tab-header p {{
            color: #666;
        }}
        .config-field {{
            margin-bottom: 20px;
            padding-bottom: 20px;
            border-bottom: 1px solid #f0f0f0;
        }}
        .config-field:last-child {{
            border-bottom: none;
        }}
        .config-field label {{
            display: block;
            font-weight: 500;
            margin-bottom: 5px;
            color: #333;
        }}
        .config-field input[type="text"],
        .config-field input[type="password"],
        .config-field input[type="number"],
        .config-field select,
        .config-field textarea {{
            width: 100%;
            padding: 10px 12px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.2s;
        }}
        .config-field input:focus,
        .config-field select:focus,
        .config-field textarea:focus {{
            outline: none;
            border-color: #2563eb;
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
        }}
        .field-desc {{
            margin-top: 5px;
            font-size: 13px;
            color: #888;
        }}
        /* Toggle switch */
        .toggle-label {{
            display: flex !important;
            align-items: center;
            cursor: pointer;
        }}
        .toggle-label input {{
            display: none;
        }}
        .toggle-slider {{
            width: 48px;
            height: 24px;
            background: #ccc;
            border-radius: 12px;
            position: relative;
            transition: background 0.3s;
            margin-right: 12px;
            flex-shrink: 0;
        }}
        .toggle-slider::before {{
            content: '';
            position: absolute;
            width: 20px;
            height: 20px;
            background: #fff;
            border-radius: 50%;
            top: 2px;
            left: 2px;
            transition: transform 0.3s;
        }}
        .toggle-label input:checked + .toggle-slider {{
            background: #2563eb;
        }}
        .toggle-label input:checked + .toggle-slider::before {{
            transform: translateX(24px);
        }}
        .actions {{
            display: flex;
            gap: 12px;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 2px solid #f0f0f0;
        }}
        .btn {{
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }}
        .btn-primary {{
            background: #2563eb;
            color: #fff;
        }}
        .btn-primary:hover {{
            background: #1d4ed8;
        }}
        .btn-secondary {{
            background: #f0f0f0;
            color: #333;
        }}
        .btn-secondary:hover {{
            background: #e0e0e0;
        }}
        .btn-danger {{
            background: #ef4444;
            color: #fff;
        }}
        .btn-danger:hover {{
            background: #dc2626;
        }}
        .status-bar {{
            display: flex;
            gap: 20px;
            margin-top: 20px;
            padding: 15px;
            background: #f0fdf4;
            border-radius: 8px;
            border: 1px solid #bbf7d0;
        }}
        .status-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
        }}
        .status-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #22c55e;
        }}
        .nav-links {{
            margin-top: 20px;
            display: flex;
            gap: 15px;
        }}
        .nav-links a {{
            color: #2563eb;
            text-decoration: none;
            font-size: 14px;
        }}
        .nav-links a:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Gateway 配置中心</h1>
            <p>管理上游连接、能力配置、上下文管理、智能增强等设置</p>
        </header>

        <div class="tabs-container">
            <div class="tabs-nav">
                {tab_nav}
            </div>

            <form id="config-form">
                {tab_panels}

                <div style="padding: 0 30px 30px;">
                    <div class="actions">
                        <button type="submit" class="btn btn-primary">保存配置</button>
                        <button type="button" class="btn btn-secondary" onclick="resetForm()">重置</button>
                        <button type="button" class="btn btn-danger" onclick="exportConfig()">导出配置</button>
                    </div>
                </div>
            </form>
        </div>

        <div class="nav-links">
            <a href="/ui">返回 Admin UI</a>
            <a href="/ui/config/client">客户端配置</a>
            <a href="/stats">统计信息</a>
        </div>
    </div>

    <script>
        // Tab switching
        document.querySelectorAll('.tab-btn').forEach(btn => {{
            btn.addEventListener('click', () => {{
                // Remove active from all tabs
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

                // Activate clicked tab
                btn.classList.add('active');
                const tabId = btn.getAttribute('data-tab');
                document.getElementById(tabId).classList.add('active');
            }});
        }});

        // Form submission
        document.getElementById('config-form').addEventListener('submit', async (e) => {{
            e.preventDefault();

            const formData = new FormData(e.target);
            const config = {{}};

            // Build nested config object
            for (const [key, value] of formData.entries()) {{
                const keys = key.split('.');
                let current = config;
                for (let i = 0; i < keys.length - 1; i++) {{
                    if (!current[keys[i]]) current[keys[i]] = {{}};
                    current = current[keys[i]];
                }}
                // Handle boolean checkboxes
                if (value === 'on') {{
                    current[keys[keys.length - 1]] = true;
                }} else {{
                    current[keys[keys.length - 1]] = value;
                }}
            }}

            // Handle unchecked checkboxes
            document.querySelectorAll('input[type="checkbox"]:not(:checked)').forEach(cb => {{
                const keys = cb.name.split('.');
                let current = config;
                for (let i = 0; i < keys.length - 1; i++) {{
                    if (!current[keys[i]]) current[keys[i]] = {{}};
                    current = current[keys[i]];
                }}
                current[keys[keys.length - 1]] = false;
            }});

            try {{
                const response = await fetch('/api/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(config),
                }});

                if (response.ok) {{
                    alert('配置已保存');
                }} else {{
                    alert('保存失败: ' + response.statusText);
                }}
            }} catch (error) {{
                alert('保存失败: ' + error.message);
            }}
        }});

        function resetForm() {{
            if (confirm('确定要重置所有配置吗？')) {{
                location.reload();
            }}
        }}

        function exportConfig() {{
            const formData = new FormData(document.getElementById('config-form'));
            const config = {{}};
            for (const [key, value] of formData.entries()) {{
                const keys = key.split('.');
                let current = config;
                for (let i = 0; i < keys.length - 1; i++) {{
                    if (!current[keys[i]]) current[keys[i]] = {{}};
                    current = current[keys[i]];
                }}
                current[keys[keys.length - 1]] = value;
            }}

            const blob = new Blob([JSON.stringify(config, null, 2)], {{ type: 'application/json' }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'gateway-config.json';
            a.click();
            URL.revokeObjectURL(url);
        }}
    </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API Handlers
# ---------------------------------------------------------------------------

def handle_config_get(config: dict[str, Any]) -> str:
    """Handle GET request for configuration UI."""
    return render_web_config_ui(config)


def handle_config_post(data: dict[str, Any], current_config: dict[str, Any]) -> dict[str, Any]:
    """Handle POST request to update configuration."""
    # Merge new config into current config
    updated = _deep_merge(current_config, data)
    return updated


def _deep_merge(base: dict, update: dict) -> dict:
    """Deep merge two dictionaries."""
    result = base.copy()
    for key, value in update.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def get_config_schema() -> list[dict[str, Any]]:
    """Get configuration schema as JSON-serializable data."""
    tabs = _get_config_tabs()
    return [
        {
            "id": tab.id,
            "label": tab.label,
            "icon": tab.icon,
            "description": tab.description,
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.field_type,
                    "description": f.description,
                    "default": f.default,
                    "required": f.required,
                    "options": f.options,
                    "min": f.min_value,
                    "max": f.max_value,
                    "placeholder": f.placeholder,
                }
                for f in tab.fields
            ],
        }
        for tab in tabs
    ]
