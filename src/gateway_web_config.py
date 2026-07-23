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
    """Return the canonical schema for the live Gateway configuration UI."""
    protocols = [
        {"value": "openai_chat", "label": "OpenAI Chat"},
        {"value": "openai_responses", "label": "OpenAI Responses"},
        {"value": "anthropic_messages", "label": "Anthropic Messages"},
    ]
    strategies = [
        {"value": "round_robin", "label": "轮询"},
        {"value": "least_connections", "label": "最少连接"},
        {"value": "random", "label": "随机"},
    ]
    return [
        ConfigTab(
            id="upstream",
            label="上游配置",
            icon="🔗",
            description="配置当前活跃上游；保存时会同步到对应 profile。",
            fields=[
                ConfigField("upstream.base_url", "Base URL", "text", required=True, placeholder="https://api.example.com"),
                ConfigField("upstream.api_key", "API Key", "password", description="留空或保持 *** 表示不修改。"),
                ConfigField("upstream.model", "模型", "text", required=True),
                ConfigField("upstream.protocol", "协议", "select", default="openai_chat", options=protocols),
                ConfigField(
                    "upstream.tools_enabled",
                    "Tools 模式",
                    "select",
                    default="adapter",
                    options=[
                        {"value": "adapter", "label": "Gateway Adapter"},
                        {"value": "native", "label": "Native"},
                        {"value": "disabled", "label": "Disabled"},
                    ],
                ),
                ConfigField("upstream.timeout_seconds", "请求超时（秒）", "number", default=60.0, min_value=0.1, max_value=600.0),
                ConfigField("upstream.retry_max_attempts", "单上游重试次数", "number", default=3, min_value=1, max_value=20),
                ConfigField("upstream.max_input_tokens", "最大输入 Token", "number", default=1048576, min_value=1000, max_value=10000000),
                ConfigField("upstream.max_output_tokens", "最大输出 Token", "number", default=131072, min_value=1, max_value=1000000),
                ConfigField("upstream.max_response_bytes", "最大响应字节", "number", default=33554432, min_value=1024, max_value=268435456),
                ConfigField("upstream.max_concurrency", "上游最大并发", "number", default=32, min_value=1, max_value=10000),
            ],
        ),
        ConfigTab(
            id="capabilities",
            label="能力配置",
            icon="⚡",
            description="声明活跃上游真实支持的协议能力。",
            fields=[
                ConfigField("upstream.capabilities.supports_streaming", "支持流式", "boolean", default=True),
                ConfigField("upstream.capabilities.supports_tools", "支持 Tools", "boolean", default=False),
                ConfigField("upstream.capabilities.supports_function_calls", "支持 Function Calls", "boolean", default=False),
                ConfigField("upstream.capabilities.supports_parallel_tool_calls", "支持并行工具调用", "boolean", default=False),
                ConfigField("upstream.capabilities.supports_vision", "支持视觉", "boolean", default=False),
                ConfigField("upstream.capabilities.supports_network", "支持网络", "boolean", default=False),
                ConfigField("upstream.capabilities.supports_web_search", "支持 Web Search", "boolean", default=False),
                ConfigField("upstream.capabilities.supports_json_schema", "支持 JSON Schema", "boolean", default=False),
            ],
        ),
        ConfigTab(
            id="context",
            label="上下文配置",
            icon="📝",
            description="配置上下文压缩、扇出、质量审查和记忆治理。",
            fields=[
                ConfigField("context.enabled", "启用上下文管理", "boolean", default=True),
                ConfigField("context.max_input_tokens", "最大输入 Token", "number", default=1048576, min_value=1000, max_value=10000000),
                ConfigField("context.keep_recent_messages", "保留最近消息", "number", default=12, min_value=1, max_value=1000),
                ConfigField("context.summary_max_chars", "摘要最大字符", "number", default=6000, min_value=100, max_value=1000000),
                ConfigField("context.fanout_enabled", "启用扇出", "boolean", default=True),
                ConfigField("context.fanout_chunk_tokens", "扇出分块 Token", "number", default=120000, min_value=1000, max_value=10000000),
                ConfigField("context.fanout_max_chunks", "最大分块数（0=不限）", "number", default=0, min_value=0, max_value=10000),
                ConfigField("context.fanout_max_workers", "扇出 Worker", "number", default=4, min_value=1, max_value=128),
                ConfigField("context.quality_review_enabled", "启用质量审查", "boolean", default=True),
                ConfigField("context.memory_enabled", "启用记忆", "boolean", default=True),
                ConfigField("context.memory_max_items", "单会话记忆上限", "number", default=200, min_value=1, max_value=100000),
            ],
        ),
        ConfigTab(
            id="intelligence",
            label="智力提升",
            icon="🧠",
            description="规则增强可独立运行；LLM provider 复用 Gateway 上游传输。",
            fields=[
                ConfigField("intelligence.enabled", "启用智力提升", "boolean", default=True),
                ConfigField("intelligence.reflection_enabled", "启用反思", "boolean", default=True),
                ConfigField("intelligence.decomposition_enabled", "启用问题分解", "boolean", default=True),
                ConfigField("intelligence.quality_assessment_enabled", "启用质量评估", "boolean", default=True),
                ConfigField("intelligence.quality_threshold", "质量阈值", "number", default=0.6, min_value=0.0, max_value=1.0),
                ConfigField("intelligence.use_llm", "启用 LLM Provider", "boolean", default=False),
                ConfigField("intelligence.provider", "Provider", "text", default="gateway_upstream", required=True),
                ConfigField("intelligence.model", "独立模型（可空）", "text", default=""),
                ConfigField("intelligence.llm_timeout", "Provider 超时（秒）", "number", default=15.0, min_value=0.1, max_value=300.0),
                ConfigField("intelligence.max_input_chars", "Provider 最大输入字符", "number", default=20000, min_value=256, max_value=200000),
                ConfigField("intelligence.temperature", "Temperature", "number", default=0.0, min_value=0.0, max_value=2.0),
                ConfigField("intelligence.strict_mode", "Strict 模式", "boolean", description="Provider 失败时阻止请求，不回退规则。", default=False),
            ],
        ),
        ConfigTab(
            id="concurrency",
            label="并发与多上游",
            icon="🚀",
            description="配置连接、选择策略、故障转移和熔断。",
            fields=[
                ConfigField("concurrency.enabled", "启用连接优化", "boolean", default=True),
                ConfigField("concurrency.max_connections", "最大连接", "number", default=100, min_value=1, max_value=10000),
                ConfigField("concurrency.max_connections_per_host", "单主机连接", "number", default=10, min_value=1, max_value=10000),
                ConfigField("concurrency.retry_count", "兼容重试次数", "number", default=2, min_value=0, max_value=20),
                ConfigField("concurrency.load_balance_strategy", "负载均衡策略", "select", default="round_robin", options=strategies),
                ConfigField("concurrency.multi_upstream_enabled", "启用多上游", "boolean", default=False),
                ConfigField("concurrency.multi_upstream_max_attempts", "最大上游尝试（0=全部）", "number", default=0, min_value=0, max_value=100),
                ConfigField("concurrency.multi_upstream_failure_threshold", "熔断失败阈值", "number", default=3, min_value=1, max_value=1000),
                ConfigField("concurrency.multi_upstream_recovery_seconds", "熔断恢复秒数", "number", default=30.0, min_value=0.1, max_value=86400.0),
            ],
        ),
        ConfigTab(
            id="cache",
            label="缓存配置",
            icon="💾",
            description="配置租户隔离的语义缓存和持久化工具缓存策略。",
            fields=[
                ConfigField("cache.enabled", "启用语义缓存", "boolean", default=True),
                ConfigField("cache.max_entries", "最大条目", "number", default=1000, min_value=1, max_value=1000000),
                ConfigField("cache.similarity_threshold", "相似度阈值", "number", default=0.92, min_value=0.0, max_value=1.0),
                ConfigField("cache.ttl_seconds", "TTL（秒）", "number", default=3600, min_value=1, max_value=31536000),
                ConfigField("cache.embedding_url", "Embedding URL", "text", default=""),
                ConfigField("cache.embedding_model", "Embedding 模型", "text", default="default"),
                ConfigField("cache.embedding_api_key", "Embedding API Key", "password", description="留空或保持 *** 表示不修改。"),
                ConfigField("gateway.tool_cache_persist_local_results", "持久化本地工具结果", "boolean", default=False),
            ],
        ),
        ConfigTab(
            id="tools",
            label="工具与执行",
            icon="🛠️",
            description="配置 Gateway-owned 工具执行边界。",
            fields=[
                ConfigField("gateway.tool_mode", "工具模式", "select", default="orchestrate", options=[
                    {"value": "orchestrate", "label": "Orchestrate"},
                    {"value": "native_passthrough", "label": "Native Passthrough"},
                    {"value": "proxy", "label": "Proxy"},
                ]),
                ConfigField("gateway.max_tool_rounds", "最大工具轮次", "number", default=10, min_value=1, max_value=100),
                ConfigField("gateway.tool_execution_timeout_seconds", "工具超时（秒）", "number", default=60.0, min_value=0.1, max_value=3600.0),
                ConfigField("gateway.allow_write_tools", "允许写工具", "boolean", default=False),
                ConfigField("gateway.allow_shell_tools", "允许 Shell 工具", "boolean", default=False),
                ConfigField("gateway.execute_user_side_tools_in_gateway", "执行下游工具", "boolean", default=False),
                ConfigField("gateway.text_tool_call_fallback_enabled", "文本 Tool Call 兜底", "boolean", default=True),
                ConfigField("gateway.local_planner_enabled", "本地 Planner", "boolean", default=True),
                ConfigField("http_actions.enabled", "启用 HTTP Actions", "boolean", default=True),
                ConfigField("assistants.db_path", "Assistants 数据库", "text", default=".gateway_runtime/assistants.sqlite3"),
                ConfigField("assistants.retention_days", "Assistants 保留天数", "number", default=30, min_value=0, max_value=36500),
                ConfigField("assistants.max_rows", "Assistants 单表最大行数", "number", default=50000, min_value=1, max_value=10000000),
            ],
        ),
        ConfigTab(
            id="web2api",
            label="Web2API",
            icon="🌐",
            description="配置受 SSRF、大小、并发和缓存边界保护的网页提取。",
            fields=[
                ConfigField("web2api.enabled", "启用 Web2API", "boolean", default=True),
                ConfigField("web2api.max_concurrent", "最大并发", "number", default=5, min_value=1, max_value=1000),
                ConfigField("web2api.cache_ttl_seconds", "缓存 TTL", "number", default=300, min_value=0, max_value=86400),
                ConfigField("web2api.request_timeout", "请求超时（秒）", "number", default=30, min_value=1, max_value=300),
                ConfigField("web2api.max_content_bytes", "最大内容字节", "number", default=5242880, min_value=1024, max_value=268435456),
                ConfigField("web2api.max_cache_entries", "最大缓存条目", "number", default=256, min_value=1, max_value=100000),
                ConfigField("web2api.user_agent", "User-Agent", "text", default="Gateway-Web2API/1.0"),
                ConfigField("web2api.allow_private_network", "允许私网", "boolean", default=False),
                ConfigField("web2api.allow_regex", "允许正则提取", "boolean", default=False),
                ConfigField("web2api.allow_raw_html", "允许返回原始 HTML", "boolean", default=False),
            ],
        ),
        ConfigTab(
            id="security",
            label="安全与入口",
            icon="🔒",
            description="配置请求认证之后的共享限流、准入和浏览器 Origin。",
            fields=[
                ConfigField("gateway.max_concurrent_requests", "Gateway 最大并发", "number", default=32, min_value=1, max_value=10000),
                ConfigField("gateway.concurrency_queue_timeout_seconds", "并发排队超时", "number", default=5.0, min_value=0.0, max_value=600.0),
                ConfigField("gateway.rate_limit_enabled", "启用下游限流", "boolean", default=True),
                ConfigField("gateway.rate_limit_rpm", "每 Key 每分钟请求", "number", default=120, min_value=1, max_value=1000000),
                ConfigField("gateway.max_request_body_bytes", "最大请求体字节", "number", default=67108864, min_value=1024, max_value=1073741824),
                ConfigField("gateway.cors_enabled", "启用 CORS", "boolean", default=False),
                ConfigField("gateway.cors_allowed_origins", "CORS Origin 白名单", "text", description="逗号分隔的精确 http(s) Origin；不支持 *。", default=""),
                ConfigField("gateway.public_base_url", "公开 Base URL", "text", default="http://127.0.0.1:8885"),
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
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)

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

def render_web_config_ui(
    config: dict[str, Any] | None = None,
    *,
    revision: str = "",
) -> str:
    """Render the complete web configuration UI."""
    if config is None:
        config = {}
    else:
        config = dict(config)
    raw_upstream = config.get("upstream")
    if isinstance(raw_upstream, dict):
        upstream = dict(raw_upstream)
        if not upstream.get("base_url") and upstream.get("url"):
            upstream["base_url"] = upstream.get("url")
        if upstream.get("timeout_seconds") is None and upstream.get("timeout") is not None:
            upstream["timeout_seconds"] = upstream.get("timeout")
        config["upstream"] = upstream

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
    revision_json = json.dumps(str(revision or ""))
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
        let configRevision = {revision_json};
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
                const response = await fetch('/api/config/update', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ config: config, revision: configRevision }}),
                }});

                if (response.ok) {{
                    const payload = await response.json();
                    configRevision = payload.revision || configRevision;
                    alert('配置已保存');
                }} else {{
                    const payload = await response.json().catch(() => ({{}}));
                    alert('保存失败: ' + (payload.error?.message || response.statusText));
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
