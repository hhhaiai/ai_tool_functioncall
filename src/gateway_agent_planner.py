#!/usr/bin/env python3
"""Outer agent planner for chat-only upstreams.

The gateway cannot rely on a chat-only upstream model to decide native tool
calls.  This module owns deterministic workflow planning, downstream tool
selection, lightweight planner state, and evidence compaction so the upstream
model can stay focused on conversation/synthesis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
import hashlib
import json
import os
import pathlib
import re
import shlex
import sqlite3
import threading
import time
import uuid

from .gateway_builtin_tools import ToolCall, _workspace_root
from .gateway_config import _config_env, _gateway_config
from .gateway_protocol import (
    _decode_tool_result_content,
    _is_responses_tool_call_type,
    _is_responses_tool_history_type,
    _is_responses_tool_output_type,
    _last_user_text,
    _legacy_function_call_id,
    _responses_tool_call_arguments_value,
    _responses_tool_call_name,
    _responses_tool_output_content,
)

Json = dict[str, Any]

PROJECT_INTENT_RE = re.compile(
    r"(分析|看下|看看|解析|梳理|理解|审查|review|analy[sz]e|inspect|understand).{0,30}(项目|工程|repo|repository|codebase|代码)",
    re.I | re.S,
)
PATH_RE = re.compile(
    r"@?(?P<path>"
    r"(?:~?/|/|\.{1,2}/)[^\s<>'\"`|]+"
    r"|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.@%+=:,/-]+"
    r"|[A-Za-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|json|jsonl|toml|yaml|yml|md|txt|sh|bash|zsh|env|ini|cfg|conf|html|css|sql|go|rs|java|kt|swift|c|cc|cpp|h|hpp)"
    r")"
)
PLANNER_CALL_ID_RE = re.compile(r"^planner_(?P<step>.+)_[0-9a-f]{32}$")
QUALIFIED_NAME_RE = re.compile(r"""["']?qualified_name["']?\s*[:=]\s*["'](?P<name>[^"']+)["']""")


def strict_agent_planner_every_turn() -> bool:
    """Return true when every communication must be planner-owned.

    The remote Mimo/chat-only deployment enables this.  Legacy local
    orchestration tests and deployments can leave it disabled so native/text
    tool loops continue to work until they opt into the stricter remote Agent
    Runtime contract.
    """
    raw = os.environ.get("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN")
    if raw is None:
        cfg = _gateway_config()
        raw = (
            cfg.get("agent_planner_strict_every_turn")
            if isinstance(cfg, dict) and "agent_planner_strict_every_turn" in cfg
            else cfg.get("strict_agent_planner") if isinstance(cfg, dict) else None
        )
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "strict"}


@dataclass
class PlannerToolEvidence:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class PlannerDecision:
    calls: list[ToolCall] = field(default_factory=list)
    workflow: str = ""
    step: str = ""
    reason: str = ""
    state: Json = field(default_factory=dict)


@dataclass
class PlannerIntent:
    """Structured current-turn intent selected by the remote Agent Planner.

    This is deliberately Gateway-owned state, not an upstream model response.
    It lets remote clients/admin APIs see why the planner picked a workflow
    without granting the chat-only upstream tool authority.
    """

    kind: str
    workflow: str
    confidence: float
    reason: str
    signals: list[str] = field(default_factory=list)
    source: str = "current_user_text"

    def to_json(self) -> Json:
        return {
            "kind": self.kind,
            "workflow": self.workflow,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason[:500],
            "signals": [str(item)[:120] for item in self.signals[:20]],
            "source": self.source,
        }


PROJECT_ANALYSIS_TRANSITIONS: list[Json] = [
    {
        "step": "planner_progress",
        "condition": "before_evidence_and_no_plan",
        "builder": "planning_tool",
        "reason": "publish project-analysis plan before collecting evidence",
    },
    {
        "step": "codebase_onboarding",
        "condition": "skill_available_before_structure",
        "builder": "codebase_onboarding_skill",
        "reason": "load project onboarding skill first",
    },
    {
        "step": "project_structure",
        "condition": "after_skill_without_structure",
        "builder": "workspace_structure",
        "reason": "collect real project structure after onboarding",
    },
    {
        "step": "core_flow_trace",
        "condition": "after_project_structure_without_core_flow",
        "builder": "core_flow_trace",
        "reason": "trace core request flow after project structure",
    },
    {
        "step": "symbol_deep_dive",
        "condition": "after_core_flow_without_symbol_deep_dive",
        "builder": "symbol_deep_dive",
        "reason": "inspect core symbols discovered by code graph",
    },
    {
        "step": "key_file_read",
        "condition": "after_structure_without_read",
        "builder": "key_file_read",
        "reason": "read key files selected from structure evidence",
    },
    {
        "step": "project_structure",
        "condition": "no_evidence_no_tools_fallback",
        "builder": "workspace_structure",
        "reason": "collect project structure without onboarding skill",
    },
]


FIX_LOOP_TRANSITIONS: list[Json] = [
    {
        "step": "diagnostic_read",
        "condition": "failure_evidence_without_read",
        "builder": "diagnostic_read",
        "reason": "read files referenced by failing tool output",
    },
    {
        "step": "source_followup_read",
        "condition": "failure_evidence_with_unread_source_imports",
        "builder": "source_followup_read",
        "reason": "read source files imported by failing tests before asking weak upstream to patch",
    },
]


QA_LOOP_TRANSITIONS: list[Json] = [
    {
        "step": "validate_after_test",
        "condition": "edit_result_requires_test_validation",
        "builder": "validate_after_edit",
        "reason": "rerun validation after edit/write",
        "kind": "test",
    },
    {
        "step": "validate_after_build",
        "condition": "edit_result_requires_build_validation",
        "builder": "validate_after_edit",
        "reason": "rerun validation after edit/write",
        "kind": "build",
    },
]


CODE_SEARCH_TRANSITIONS: list[Json] = [
    {
        "step": "code_search",
        "condition": "code_search_without_existing_search",
        "builder": "code_search",
        "reason": "search code with declared code/MCP tools",
    },
]


TEST_BUILD_TRANSITIONS: list[Json] = [
    {
        "step": "run_test",
        "condition": "validation_test_without_existing_run",
        "builder": "run_validation",
        "reason": "run inferred test command",
        "kind": "test",
    },
    {
        "step": "run_build",
        "condition": "validation_build_without_existing_run",
        "builder": "run_validation",
        "reason": "run inferred build command",
        "kind": "build",
    },
]


GENERIC_TOOL_TRANSITIONS: list[Json] = [
    {
        "step": "skill_request",
        "condition": "skill_request_without_evidence",
        "builder": "skill_request",
        "reason": "explicit skill request",
    },
    {
        "step": "shell_command",
        "condition": "shell_command_without_evidence",
        "builder": "shell_command",
        "reason": "explicit shell command request",
    },
    {
        "step": "read_file",
        "condition": "read_file_without_evidence",
        "builder": "read_file",
        "reason": "read/analyze mentioned file",
    },
    {
        "step": "list_directory",
        "condition": "list_directory_without_evidence",
        "builder": "list_directory",
        "reason": "list directory request",
    },
    {
        "step": "web_search",
        "condition": "web_search_without_evidence",
        "builder": "web_search",
        "reason": "web search request",
    },
    {
        "step": "custom_function",
        "condition": "custom_function_without_evidence",
        "builder": "custom_function",
        "reason": "declared custom function matches user intent",
    },
]


EDIT_TRANSITIONS: list[Json] = [
    {
        "step": "edit_file",
        "condition": "bounded_edit_without_evidence",
        "builder": "edit_file",
        "reason": "explicit bounded edit request",
    },
    {
        "step": "write_file",
        "condition": "bounded_write_without_evidence",
        "builder": "write_file",
        "reason": "explicit bounded write request",
    },
]


WORKFLOW_REGISTRY: dict[str, Json] = {
    "project_analysis": {
        "owner": "agent_planner",
        "description": "Analyze a client workspace using skills, code graph, source reads, compact evidence, and chat-only synthesis.",
        "steps": ["planner_progress", "codebase_onboarding", "project_structure", "core_flow_trace", "symbol_deep_dive", "key_file_read", "synthesis"],
        "plan_items": [
            {"step": "加载项目分析技能/上下文规则", "status": "in_progress"},
            {"step": "收集真实项目结构和关键文件", "status": "pending"},
            {"step": "压缩证据并生成最终分析", "status": "pending"},
        ],
        "transitions": PROJECT_ANALYSIS_TRANSITIONS,
    },
    "generic_tool": {
        "owner": "agent_planner",
        "description": "Route explicit user tool intents to downstream client tools or caller-declared functions.",
        "steps": ["skill_request", "shell_command", "read_file", "list_directory", "web_search", "custom_function", "synthesis"],
        "plan_items": [
            {"step": "解析用户意图", "status": "in_progress"},
            {"step": "调用必要工具收集证据", "status": "pending"},
            {"step": "基于证据回复用户", "status": "pending"},
        ],
        "transitions": GENERIC_TOOL_TRANSITIONS,
    },
    "code_search": {
        "owner": "agent_planner",
        "description": "Search code using codebase-memory/MCP or local search tools, then synthesize from evidence.",
        "steps": ["planner_progress", "code_search", "synthesis"],
        "plan_items": [
            {"step": "用代码索引/MCP 或本地搜索定位相关实现", "status": "in_progress"},
            {"step": "读取关键代码并整理调用关系", "status": "pending"},
            {"step": "基于证据总结结论", "status": "pending"},
        ],
        "transitions": CODE_SEARCH_TRANSITIONS,
    },
    "test_build": {
        "owner": "agent_planner",
        "description": "Run test/build commands in the client workspace and synthesize from validation evidence.",
        "steps": ["planner_progress", "run_test", "run_build", "synthesis"],
        "plan_items": [
            {"step": "运行测试或构建以获得真实失败/通过证据", "status": "in_progress"},
            {"step": "读取失败相关源码并定位原因", "status": "pending"},
            {"step": "应用最小修复并重新验证", "status": "pending"},
        ],
        "transitions": TEST_BUILD_TRANSITIONS,
    },
    "fix_loop": {
        "owner": "agent_planner",
        "description": "Diagnose failed tests/builds with source reads, deny chat-only patch authority, and verify downstream edits.",
        "steps": ["planner_progress", "run_test", "diagnostic_read", "source_followup_read", "synthesis"],
        "plan_items": [
            {"step": "运行测试或构建以获得真实失败/通过证据", "status": "in_progress"},
            {"step": "读取失败相关源码并定位原因", "status": "pending"},
            {"step": "应用最小修复并重新验证", "status": "pending"},
        ],
        "transitions": FIX_LOOP_TRANSITIONS,
    },
    "qa_loop": {
        "owner": "agent_planner",
        "description": "Re-run validation after downstream edit/write results and synthesize only after evidence is available.",
        "steps": ["validate_after_test", "validate_after_build", "synthesis"],
        "plan_items": [
            {"step": "运行测试或构建以获得真实失败/通过证据", "status": "in_progress"},
            {"step": "读取失败相关源码并定位原因", "status": "pending"},
            {"step": "应用最小修复并重新验证", "status": "pending"},
        ],
        "transitions": QA_LOOP_TRANSITIONS,
    },
    "edit": {
        "owner": "agent_planner",
        "description": "Surface bounded user-requested file edits/writes to the downstream client workspace.",
        "steps": ["planner_progress", "edit_file", "write_file", "validate_after_test"],
        "plan_items": [
            {"step": "确认编辑边界和目标文件", "status": "in_progress"},
            {"step": "执行声明式文件修改", "status": "pending"},
            {"step": "按需验证修改结果", "status": "pending"},
        ],
        "transitions": EDIT_TRANSITIONS,
    },
    "gateway_owned_tool": {
        "owner": "gateway_service",
        "description": "Preexecute Gateway-owned pure/network/connector tools, then ask chat-only upstream to synthesize.",
        "steps": ["preexecute_gateway_owned_tool", "synthesis"],
        "plan_items": [
            {"step": "执行 Gateway-owned service tool", "status": "in_progress"},
            {"step": "把工具结果注入 chat-only synthesis", "status": "pending"},
        ],
    },
    "chat_only_synthesis": {
        "owner": "agent_planner",
        "description": "Final synthesis boundary where upstream tool attempts are ignored and logged.",
        "steps": ["ignore_upstream_tool_attempt", "synthesis"],
        "plan_items": [
            {"step": "注入 Planner evidence", "status": "in_progress"},
            {"step": "chat-only upstream 只生成最终表达", "status": "pending"},
        ],
    },
}


def planner_workflow_catalog() -> list[Json]:
    """Return the Agent Planner workflow registry as a bounded public catalog."""
    catalog: list[Json] = []
    for name, spec in WORKFLOW_REGISTRY.items():
        entry: Json = {
            "name": name,
            "owner": str(spec.get("owner") or "agent_planner"),
            "description": str(spec.get("description") or "")[:500],
            "steps": [str(step) for step in (spec.get("steps") if isinstance(spec.get("steps"), list) else [])],
            "plan_items": [
                {
                    "step": str(item.get("step") or ""),
                    "status": str(item.get("status") or "pending"),
                }
                for item in (spec.get("plan_items") if isinstance(spec.get("plan_items"), list) else [])
                if isinstance(item, dict)
            ],
        }
        transitions = spec.get("transitions")
        if isinstance(transitions, list):
            entry["transitions"] = [
                {
                    "step": str(item.get("step") or ""),
                    "condition": str(item.get("condition") or ""),
                    "builder": str(item.get("builder") or ""),
                    "reason": str(item.get("reason") or "")[:300],
                }
                for item in transitions
                if isinstance(item, dict)
            ]
        catalog.append(entry)
    return catalog


def _workflow_plan_items(workflow: str, user_text: str) -> list[Json]:
    spec = WORKFLOW_REGISTRY.get(workflow or "generic_tool") or WORKFLOW_REGISTRY["generic_tool"]
    items = spec.get("plan_items") if isinstance(spec.get("plan_items"), list) else []
    copied = [
        {
            "step": str(item.get("step") or ""),
            "status": str(item.get("status") or "pending"),
        }
        for item in items
        if isinstance(item, dict)
    ]
    if workflow in {"", "generic_tool"} and user_text and copied:
        copied[0]["step"] = (user_text or copied[0]["step"])[:80]
    return copied


INTENT_REGISTRY: dict[str, Json] = {
    "project_analysis": {
        "workflow": "project_analysis",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Inspect/analyze a client workspace through planner-managed skills, code graph, reads, and synthesis.",
    },
    "validation": {
        "workflow": "test_build",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Run tests/builds in the client workspace and use failures as diagnostic evidence.",
    },
    "code_search": {
        "workflow": "code_search",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Search code with declared code/MCP/search tools before synthesis.",
    },
    "edit": {
        "workflow": "edit",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Surface explicit bounded edits to downstream client tools.",
    },
    "write": {
        "workflow": "edit",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Surface explicit bounded file writes to downstream client tools.",
    },
    "skill_request": {
        "workflow": "generic_tool",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Load or list downstream client skills.",
    },
    "shell_command": {
        "workflow": "generic_tool",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Run explicit shell commands in the client workspace.",
    },
    "read_file": {
        "workflow": "generic_tool",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Read files from the client workspace through declared downstream tools.",
    },
    "list_directory": {
        "workflow": "generic_tool",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "List client workspace directories through declared downstream tools or shell fallback.",
    },
    "web_search": {
        "workflow": "generic_tool",
        "owner": "agent_planner",
        "dispatch": "gateway_or_downstream_declared_tool",
        "description": "Route explicit web-search intent to a declared WebSearch/search capability.",
    },
    "custom_function": {
        "workflow": "generic_tool",
        "owner": "agent_planner",
        "dispatch": "downstream_client",
        "description": "Match caller-private declared functions by name/description without giving upstream tool authority.",
    },
    "plain_chat": {
        "workflow": "chat_only_synthesis",
        "owner": "agent_planner",
        "dispatch": "none",
        "description": "No planner tool intent matched; let chat-only upstream synthesize normally.",
    },
}


def planner_intent_catalog() -> list[Json]:
    """Return the Agent Planner intent registry as a bounded public catalog."""
    catalog: list[Json] = []
    for kind, spec in INTENT_REGISTRY.items():
        catalog.append({
            "kind": kind,
            "workflow": str(spec.get("workflow") or ""),
            "owner": str(spec.get("owner") or "agent_planner"),
            "dispatch": str(spec.get("dispatch") or ""),
            "description": str(spec.get("description") or "")[:500],
        })
    return catalog


class AgentPlannerStore:
    """Small sqlite-backed planner memory for the remote Agent runtime.

    It stores compact evidence and step markers keyed by tenant/session/workspace
    so multi-round tool workflows do not depend on the chat-only upstream
    remembering how to plan.  It must be safe under concurrent remote requests.
    """

    def __init__(self, path: pathlib.Path | None = None) -> None:
        runtime_dir = pathlib.Path(_config_env("GATEWAY_RUNTIME_DIR", ".gateway_runtime") or ".gateway_runtime")
        self.path = path or runtime_dir / "agent_planner.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30.0)
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS planner_sessions (
                        session_key TEXT PRIMARY KEY,
                        tenant_key TEXT NOT NULL DEFAULT '',
                        workspace_key TEXT NOT NULL DEFAULT '',
                        workflow TEXT NOT NULL DEFAULT '',
                        current_step TEXT NOT NULL DEFAULT '',
                        evidence_count INTEGER NOT NULL DEFAULT 0,
                        state_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                columns = {str(row[1]) for row in con.execute("PRAGMA table_info(planner_sessions)").fetchall()}
                migrations = {
                    "tenant_key": "ALTER TABLE planner_sessions ADD COLUMN tenant_key TEXT NOT NULL DEFAULT ''",
                    "workspace_key": "ALTER TABLE planner_sessions ADD COLUMN workspace_key TEXT NOT NULL DEFAULT ''",
                    "workflow": "ALTER TABLE planner_sessions ADD COLUMN workflow TEXT NOT NULL DEFAULT ''",
                    "current_step": "ALTER TABLE planner_sessions ADD COLUMN current_step TEXT NOT NULL DEFAULT ''",
                    "evidence_count": "ALTER TABLE planner_sessions ADD COLUMN evidence_count INTEGER NOT NULL DEFAULT 0",
                }
                for column, sql in migrations.items():
                    if column not in columns:
                        con.execute(sql)
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_planner_sessions_tenant_workspace_updated "
                    "ON planner_sessions(tenant_key, workspace_key, updated_at DESC)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_planner_sessions_workflow_step_updated "
                    "ON planner_sessions(workflow, current_step, updated_at DESC)"
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS runtime_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        session_key TEXT NOT NULL DEFAULT '',
                        tenant_key TEXT NOT NULL DEFAULT '',
                        workspace_key TEXT NOT NULL DEFAULT '',
                        event_type TEXT NOT NULL,
                        workflow TEXT NOT NULL DEFAULT '',
                        step TEXT NOT NULL DEFAULT '',
                        summary TEXT NOT NULL DEFAULT '',
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_runtime_events_scope_ts "
                    "ON runtime_events(tenant_key, workspace_key, session_key, ts DESC)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_runtime_events_type_ts "
                    "ON runtime_events(event_type, workflow, step, ts DESC)"
                )
                self._backfill_index_fields(con)

    def _backfill_index_fields(self, con: sqlite3.Connection, *, limit: int = 5000) -> None:
        rows = con.execute(
            """
            SELECT session_key, state_json
            FROM planner_sessions
            WHERE tenant_key='' OR workspace_key='' OR workflow='' OR current_step=''
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for session_key, state_json in rows:
            try:
                state = json.loads(state_json)
            except Exception:
                state = {}
            fields = _planner_index_fields(str(session_key or ""), state if isinstance(state, dict) else {})
            con.execute(
                """
                UPDATE planner_sessions
                SET tenant_key=?, workspace_key=?, workflow=?, current_step=?, evidence_count=?
                WHERE session_key=?
                """,
                (
                    fields["tenant_key"],
                    fields["workspace_key"],
                    fields["workflow"],
                    fields["current_step"],
                    fields["evidence_count"],
                    str(session_key or ""),
                ),
            )

    def _insert_event(
        self,
        con: sqlite3.Connection,
        *,
        session_key: str,
        tenant_key: str,
        workspace_key: str,
        event_type: str,
        workflow: str = "",
        step: str = "",
        summary: str = "",
        metadata: Json | None = None,
        ts: float | None = None,
    ) -> None:
        con.execute(
            """
            INSERT INTO runtime_events(
                ts, session_key, tenant_key, workspace_key, event_type, workflow, step, summary, metadata_json
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                float(ts or time.time()),
                str(session_key or ""),
                str(tenant_key or ""),
                str(workspace_key or ""),
                str(event_type or ""),
                str(workflow or ""),
                str(step or ""),
                str(summary or "")[:2000],
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    def load(self, session_key: str) -> Json:
        try:
            with self._lock:
                with self._connect() as con:
                    row = con.execute(
                        "SELECT state_json FROM planner_sessions WHERE session_key=?",
                        (session_key,),
                    ).fetchone()
        except sqlite3.OperationalError:
            self._init_db()
            return {}
        if not row:
            return {}
        try:
            parsed = json.loads(row[0])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def save(self, session_key: str, state: Json) -> None:
        state = dict(state or {})
        state.setdefault("session_key", str(session_key or ""))
        fields = _planner_index_fields(str(session_key or ""), state)
        state.setdefault("tenant_key", fields["tenant_key"])
        state.setdefault("workspace_key", fields["workspace_key"])
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        now = time.time()
        try:
            with self._lock:
                with self._connect() as con:
                    con.execute(
                        """
                        INSERT INTO planner_sessions(
                            session_key, tenant_key, workspace_key, workflow, current_step,
                            evidence_count, state_json, updated_at
                        )
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(session_key) DO UPDATE SET
                            tenant_key=excluded.tenant_key,
                            workspace_key=excluded.workspace_key,
                            workflow=excluded.workflow,
                            current_step=excluded.current_step,
                            evidence_count=excluded.evidence_count,
                            state_json=excluded.state_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            str(session_key or ""),
                            fields["tenant_key"],
                            fields["workspace_key"],
                            fields["workflow"],
                            fields["current_step"],
                            fields["evidence_count"],
                            payload,
                            now,
                        ),
                    )
                    self._insert_event(
                        con,
                        session_key=str(session_key or ""),
                        tenant_key=fields["tenant_key"],
                        workspace_key=fields["workspace_key"],
                        event_type="planner_state",
                        workflow=fields["workflow"],
                        step=fields["current_step"],
                        summary=f"{fields['workflow']}:{fields['current_step']}",
                        metadata={"evidence_count": fields["evidence_count"]},
                        ts=now,
                    )
        except sqlite3.OperationalError:
            self._init_db()
            with self._lock:
                with self._connect() as con:
                    con.execute(
                        """
                        INSERT INTO planner_sessions(
                            session_key, tenant_key, workspace_key, workflow, current_step,
                            evidence_count, state_json, updated_at
                        )
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(session_key) DO UPDATE SET
                            tenant_key=excluded.tenant_key,
                            workspace_key=excluded.workspace_key,
                            workflow=excluded.workflow,
                            current_step=excluded.current_step,
                            evidence_count=excluded.evidence_count,
                            state_json=excluded.state_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            str(session_key or ""),
                            fields["tenant_key"],
                            fields["workspace_key"],
                            fields["workflow"],
                            fields["current_step"],
                            fields["evidence_count"],
                            payload,
                            now,
                        ),
                    )
                    self._insert_event(
                        con,
                        session_key=str(session_key or ""),
                        tenant_key=fields["tenant_key"],
                        workspace_key=fields["workspace_key"],
                        event_type="planner_state",
                        workflow=fields["workflow"],
                        step=fields["current_step"],
                        summary=f"{fields['workflow']}:{fields['current_step']}",
                        metadata={"evidence_count": fields["evidence_count"]},
                        ts=now,
                    )

    def record_event(
        self,
        *,
        session_key: str = "",
        tenant_key: str = "",
        workspace_key: str = "",
        event_type: str,
        workflow: str = "",
        step: str = "",
        summary: str = "",
        metadata: Json | None = None,
    ) -> None:
        try:
            with self._lock:
                with self._connect() as con:
                    self._insert_event(
                        con,
                        session_key=session_key,
                        tenant_key=tenant_key,
                        workspace_key=workspace_key,
                        event_type=event_type,
                        workflow=workflow,
                        step=step,
                        summary=summary,
                        metadata=metadata,
                    )
        except sqlite3.OperationalError:
            self._init_db()

    def list_events(
        self,
        limit: int = 100,
        *,
        tenant_contains: str | None = None,
        workspace_contains: str | None = None,
        session_contains: str | None = None,
        event_type: str | None = None,
        workflow: str | None = None,
        step: str | None = None,
    ) -> list[Json]:
        try:
            limit = max(1, min(int(limit or 100), 500))
        except (TypeError, ValueError):
            limit = 100
        where: list[str] = []
        params: list[Any] = []
        if tenant_contains:
            where.append("LOWER(tenant_key) LIKE ?")
            params.append(f"%{str(tenant_contains).strip().lower()}%")
        if workspace_contains:
            where.append("LOWER(workspace_key) LIKE ?")
            params.append(f"%{str(workspace_contains).strip().lower()}%")
        if session_contains:
            where.append("LOWER(session_key) LIKE ?")
            params.append(f"%{str(session_contains).strip().lower()}%")
        if event_type:
            where.append("event_type = ?")
            params.append(str(event_type).strip())
        if workflow:
            where.append("workflow = ?")
            params.append(str(workflow).strip())
        if step:
            where.append("step = ?")
            params.append(str(step).strip())
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        try:
            with self._lock:
                with self._connect() as con:
                    rows = con.execute(
                        f"""
                        SELECT id, ts, session_key, tenant_key, workspace_key, event_type, workflow, step, summary, metadata_json
                        FROM runtime_events
                        {where_sql}
                        ORDER BY ts DESC, id DESC
                        LIMIT ?
                        """,
                        (*params, limit),
                    ).fetchall()
        except sqlite3.OperationalError:
            self._init_db()
            return []
        events: list[Json] = []
        for row in rows:
            try:
                metadata = json.loads(row[9])
            except Exception:
                metadata = {}
            events.append({
                "id": row[0],
                "ts": float(row[1] or 0),
                "session_key": row[2],
                "tenant_key": row[3],
                "workspace_key": row[4],
                "event_type": row[5],
                "workflow": row[6],
                "step": row[7],
                "summary": row[8],
                "metadata": metadata if isinstance(metadata, dict) else {},
            })
        return events

    def list_recent(
        self,
        limit: int = 50,
        *,
        workflow: str | None = None,
        current_step: str | None = None,
        session_contains: str | None = None,
        tenant_contains: str | None = None,
        workspace_contains: str | None = None,
        has_evidence: bool | None = None,
        max_scan: int | None = None,
    ) -> list[Json]:
        del max_scan  # retained for backward-compatible callers; SQL filters no longer need scan widening.
        try:
            limit = max(1, min(int(limit or 50), 500))
        except (TypeError, ValueError):
            limit = 50
        workflow_filter = str(workflow or "").strip()
        step_filter = str(current_step or "").strip()
        session_filter = str(session_contains or "").strip().lower()
        tenant_filter = str(tenant_contains or "").strip().lower()
        workspace_filter = str(workspace_contains or "").strip().lower()
        where: list[str] = []
        params: list[Any] = []
        if workflow_filter:
            where.append("workflow=?")
            params.append(workflow_filter)
        if step_filter:
            where.append("current_step=?")
            params.append(step_filter)
        if session_filter:
            where.append("LOWER(session_key) LIKE ?")
            params.append(f"%{session_filter}%")
        if tenant_filter:
            where.append("LOWER(tenant_key) LIKE ?")
            params.append(f"%{tenant_filter}%")
        if workspace_filter:
            where.append("LOWER(workspace_key) LIKE ?")
            params.append(f"%{workspace_filter}%")
        if has_evidence is True:
            where.append("evidence_count > 0")
        elif has_evidence is False:
            where.append("evidence_count <= 0")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        try:
            with self._lock:
                with self._connect() as con:
                    rows = con.execute(
                        f"""
                        SELECT session_key, state_json, updated_at, tenant_key, workspace_key, workflow, current_step, evidence_count
                        FROM planner_sessions
                        {where_sql}
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """,
                        (*params, limit),
                    ).fetchall()
        except sqlite3.OperationalError:
            self._init_db()
            return []
        sessions: list[Json] = []
        for session_key, state_json, updated_at, tenant_key, workspace_key, row_workflow, row_step, row_evidence_count in rows:
            try:
                state = json.loads(state_json)
            except Exception:
                state = {}
            snapshot = planner_state_snapshot(state if isinstance(state, dict) else {})
            if not snapshot:
                snapshot = {"session_key": str(session_key or "")}
            if not snapshot.get("session_key"):
                snapshot["session_key"] = str(session_key or "")
            snapshot.setdefault("workflow", str(row_workflow or ""))
            snapshot.setdefault("current_step", str(row_step or ""))
            snapshot["tenant_key"] = str(tenant_key or "")
            snapshot["workspace_key"] = str(workspace_key or "")
            snapshot["evidence_count"] = int(row_evidence_count or snapshot.get("evidence_count") or 0)
            snapshot["updated_at"] = float(updated_at or 0)
            sessions.append(snapshot)
        return sessions


def _session_key_index_parts(session_key: str) -> Json:
    text = str(session_key or "")
    tenant = ""
    workspace = ""
    if ":tenant:" in text:
        prefix, _, tail = text.partition(":tenant:")
        if ":" in prefix:
            _, workspace = prefix.split(":", 1)
        else:
            workspace = prefix
        markers = (":conversation_id:", ":thread_id:", ":session_id:", ":session:", ":user_id:", ":user:", ":anon:")
        marker_positions = [(tail.find(marker), marker) for marker in markers if tail.find(marker) >= 0]
        if marker_positions:
            pos, marker = min(marker_positions, key=lambda item: item[0])
            tenant = tail[:pos]
            session_kind = marker.strip(":")
            session_part = tail[pos + len(marker):]
        else:
            tenant = tail
            session_kind = ""
            session_part = ""
    else:
        session_kind = ""
        session_part = ""
    return {
        "tenant_key": tenant,
        "workspace_key": workspace,
        "session_kind": session_kind,
        "session_part": session_part,
    }


def _planner_index_fields(session_key: str, state: Json) -> Json:
    state = state if isinstance(state, dict) else {}
    parsed = _session_key_index_parts(session_key)

    def _text(name: str, fallback: str = "") -> str:
        value = state.get(name)
        value = str(value if value is not None else fallback).strip()
        return value

    try:
        evidence_count = int(state.get("evidence_count") or 0)
    except (TypeError, ValueError):
        evidence_count = 0
    return {
        "tenant_key": _text("tenant_key", parsed.get("tenant_key") or ""),
        "workspace_key": _text("workspace_key", parsed.get("workspace_key") or ""),
        "workflow": _text("workflow"),
        "current_step": _text("current_step"),
        "evidence_count": max(0, evidence_count),
    }


_STORE: AgentPlannerStore | None = None
_STORE_LOCK = threading.RLock()


def _store() -> AgentPlannerStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None or not _STORE.path.parent.exists():
            _STORE = AgentPlannerStore()
        return _STORE


def record_runtime_event(
    *,
    session_key: str = "",
    tenant_key: str = "",
    workspace_key: str = "",
    event_type: str,
    workflow: str = "",
    step: str = "",
    summary: str = "",
    metadata: Json | None = None,
) -> None:
    _store().record_event(
        session_key=session_key,
        tenant_key=tenant_key,
        workspace_key=workspace_key,
        event_type=event_type,
        workflow=workflow,
        step=step,
        summary=summary,
        metadata=metadata,
    )


def list_runtime_events(limit: int = 100, **filters: Any) -> list[Json]:
    return _store().list_events(limit, **filters)


def _runtime_scope_from_state(state: Json) -> Json:
    session_key = str((state or {}).get("session_key") or "")
    fields = _planner_index_fields(session_key, state if isinstance(state, dict) else {})
    return {
        "session_key": session_key,
        "tenant_key": fields["tenant_key"],
        "workspace_key": fields["workspace_key"],
    }


def _record_planner_runtime_event(
    state: Json,
    *,
    event_type: str,
    workflow: str = "",
    step: str = "",
    summary: str = "",
    metadata: Json | None = None,
) -> None:
    if not isinstance(state, dict) or not state.get("session_key"):
        return
    try:
        scope = _runtime_scope_from_state(state)
        record_runtime_event(
            session_key=scope["session_key"],
            tenant_key=scope["tenant_key"],
            workspace_key=scope["workspace_key"],
            event_type=event_type,
            workflow=workflow or str(state.get("workflow") or ""),
            step=step or str(state.get("current_step") or ""),
            summary=summary,
            metadata=metadata,
        )
    except Exception:
        pass


def _record_tool_dispatch_event(state: Json, calls: list[ToolCall], workflow: str, step: str, reason: str) -> None:
    if not calls:
        return
    _record_planner_runtime_event(
        state,
        event_type="tool_dispatch",
        workflow=workflow,
        step=step,
        summary=f"dispatch {len(calls)} tool(s) for {workflow}:{step}",
        metadata={
            "reason": reason,
            "calls": [
                {"id": call.call_id, "name": call.name, "metadata": call.raw}
                for call in calls
            ],
        },
    )


def _append_decision_history(state: Json, calls: list[ToolCall], workflow: str, step: str, reason: str) -> None:
    """Append a bounded planner decision record to persisted workflow state."""
    if not isinstance(state, dict):
        return
    history = state.get("decision_history")
    items: list[Json] = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    entry: Json = {
        "workflow": workflow,
        "step": step,
        "reason": reason,
        "call_count": len(calls),
        "calls": [
            {
                "id": call.call_id,
                "name": call.name,
                "arguments_preview": _json_dumps(call.arguments)[:500],
            }
            for call in calls[:10]
        ],
        "ts": time.time(),
    }
    items.append(entry)
    state["decision_history"] = items[-20:]
    state["last_decision"] = entry


def _planner_decision(calls: list[ToolCall], workflow: str, step: str, reason: str, state: Json) -> PlannerDecision:
    _append_decision_history(state, calls, workflow, step, reason)
    session_key = str(state.get("session_key") or "") if isinstance(state, dict) else ""
    if session_key:
        try:
            _store().save(session_key, state)
        except Exception:
            pass
    _record_tool_dispatch_event(state, calls, workflow, step, reason)
    return PlannerDecision(calls, workflow, step, reason, state)


def _bounded_intent_snapshot(item: Any) -> Json:
    if not isinstance(item, dict):
        return {}
    try:
        confidence = float(item.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "kind": str(item.get("kind") or "")[:80],
        "workflow": str(item.get("workflow") or "")[:80],
        "confidence": confidence,
        "reason": str(item.get("reason") or "")[:300],
        "signals": [
            str(signal)[:120]
            for signal in (item.get("signals") if isinstance(item.get("signals"), list) else [])[:20]
        ],
        "source": str(item.get("source") or "")[:80],
    }


def planner_state_snapshot(state: Json, *, max_summary_chars: int = 1200) -> Json:
    """Return a compact, serializable planner-state snapshot for clients.

    The full sqlite state can contain long evidence summaries.  This bounded
    snapshot is safe to expose in gateway_context so remote clients can observe
    workflow progress without relying on service-local logs.
    """
    if not isinstance(state, dict) or not state:
        return {}
    summary = str(state.get("evidence_summary") or "")

    def _int_field(name: str) -> int:
        try:
            return int(state.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    completed = state.get("completed_steps")
    completed_steps = [str(item) for item in completed if str(item or "").strip()] if isinstance(completed, list) else []
    decision_history = state.get("decision_history")
    bounded_decisions: list[Json] = []
    if isinstance(decision_history, list):
        for item in decision_history[-10:]:
            if not isinstance(item, dict):
                continue
            bounded_decisions.append({
                "workflow": str(item.get("workflow") or ""),
                "step": str(item.get("step") or ""),
                "reason": str(item.get("reason") or "")[:300],
                "call_count": _safe_int(item.get("call_count")),
                "calls": [
                    {
                        "id": str(call.get("id") or ""),
                        "name": str(call.get("name") or ""),
                    }
                    for call in (item.get("calls") if isinstance(item.get("calls"), list) else [])[:10]
                    if isinstance(call, dict)
                ],
                "ts": float(item.get("ts") or 0),
            })
    snapshot: Json = {
        "workflow": str(state.get("workflow") or ""),
        "current_step": str(state.get("current_step") or ""),
        "completed_steps": completed_steps,
        "evidence_count": _int_field("evidence_count"),
        "evidence_summary_chars": len(summary),
        "compaction_count": _int_field("compaction_count"),
        "llm_compaction_count": _int_field("llm_compaction_count"),
    }
    if bounded_decisions:
        snapshot["decision_history"] = bounded_decisions
        snapshot["last_decision"] = bounded_decisions[-1]
    intent = _bounded_intent_snapshot(state.get("intent"))
    if intent:
        snapshot["intent"] = intent
    intent_history = state.get("intent_history")
    bounded_intents: list[Json] = []
    if isinstance(intent_history, list):
        bounded_intents = [
            bounded
            for bounded in (_bounded_intent_snapshot(item) for item in intent_history[-10:])
            if bounded
        ]
    if bounded_intents:
        snapshot["intent_history"] = bounded_intents
    session_key = str(state.get("session_key") or "")
    if session_key:
        snapshot["session_key"] = session_key
    if summary:
        snapshot["evidence_summary_preview"] = summary[:max_summary_chars]
    return snapshot


def _json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(obj)


def _json_object_from_maybe_string(value: Any) -> Json:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _body_messages(body: Json) -> list[Json]:
    messages = body.get("messages") or body.get("input") or []
    return messages if isinstance(messages, list) else []


CLIENT_INJECTED_CONTEXT_RE = re.compile(
    r"(?is)"
    r"<system-reminder>.*?</system-reminder>"
    r"|<context_guidance>.*?</context_guidance>"
)


def _strip_client_injected_context(text: str) -> str:
    """Remove client/runtime injected guidance before intent classification.

    Claude/Codex-compatible clients can send startup hooks, PreToolUse hints,
    global CLAUDE.md/AGENTS.md content, and similar runtime reminders as
    ``role=user`` blocks.  Those blocks are context for the downstream model,
    not the human's current instruction.  If the outer Agent Planner treats
    them as the latest user request, harmless input such as ``jo`` can be
    misclassified as "run tests" because a global instruction says to run
    lint/typecheck/tests after changes.

    Important: clients often wrap a reminder and the real user text in the same
    content string, for example ``<system-reminder>...</system-reminder>\njo``.
    Strip the injected block first and preserve the trailing human text.
    """
    text = str(text or "")
    if not text:
        return ""
    text = CLIENT_INJECTED_CONTEXT_RE.sub("\n", text)
    kept: list[str] = []
    skip_until_blank = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if not line:
            skip_until_blank = False
            continue
        if skip_until_blank:
            continue
        # One-line runtime hooks should not swallow the next real user line.
        if (
            line.startswith("PreToolUse:")
            or line.startswith("PostToolUse:")
            or line.startswith("SessionStart:")
            or line.startswith("SessionEnd:")
            or line.startswith("UserPromptSubmit:")
            or line.startswith("Stop:")
        ):
            continue
        # Malformed or long injected sections are skipped until the next blank.
        if (
            line.startswith("<system-reminder>")
            or line.startswith("</system-reminder>")
            or line.startswith("<context_guidance>")
            or line.startswith("</context_guidance>")
            or line.startswith("# claudeMd")
            or "hook additional context" in line
            or lower.startswith("codebase and user instructions are shown below")
            or (lower.startswith("contents of ") and ("claude.md" in lower or "agents.md" in lower))
        ):
            skip_until_blank = True
            continue
        kept.append(raw_line)
    return "\n".join(part for part in kept if part.strip()).strip()


def _looks_like_client_injected_text(text: str) -> bool:
    stripped = str(text or "").lstrip()
    if not stripped:
        return False
    lower = stripped[:600].lower()
    return (
        stripped.startswith("<system-reminder>")
        or stripped.startswith("<context_guidance>")
        or stripped.startswith("PreToolUse:")
        or stripped.startswith("PostToolUse:")
        or stripped.startswith("SessionStart:")
        or stripped.startswith("UserPromptSubmit:")
        or "hook additional context" in lower
        or "# claudemd" in lower
        or "codebase and user instructions are shown below" in lower
    )


def _text_from_non_tool_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "tool_result" or _is_responses_tool_history_type(item.get("type")):
                    continue
                if item.get("type") in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text") or ""))
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return ""


def _strip_recalled_memory_blocks(text: str) -> str:
    """Remove Gateway-injected memory blocks before planner intent parsing.

    Infinite-context recall is valuable evidence for final synthesis, but it is
    not the user's current instruction.  If planner intent detection sees old
    rollup text such as "read README.md" before a new "hi" request, it can
    dispatch stale workspace tools.  Keep this sanitizer local to planning.

    The memory injector may concatenate memory and current user content into a
    single text block.  When the first non-memory line appears, keep it; do not
    drop the actual current instruction such as ``分析这套项目``.
    """
    if not text or "[Gateway recalled memory]" not in text:
        return text or ""
    kept: list[str] = []
    skipping = False
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line == "[Gateway recalled memory]":
            skipping = True
            continue
        if skipping:
            if not line:
                skipping = False
                continue
            if line == "[Conversation Memories]" or line.startswith("- "):
                continue
            # Memory summaries can be multi-line.  Treat all non-blank lines
            # inside the recalled-memory block as memory until the blank line
            # inserted by _memory_block separates the current user text.
            continue
        kept.append(raw_line)
    return "\n".join(part for part in kept if part.strip()).strip()


def _bounded_planner_text(text: str, *, limit: int = 12000) -> str:
    """Bound intent-classification input for remote long-context requests."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + "\n...[planner input truncated]...\n" + text[-half:]


def _planner_user_text(path: str, body: Json) -> str:
    """Return the current human instruction, excluding client injected blocks."""
    if "/responses" in path and isinstance(body.get("input"), str):
        return _strip_client_injected_context(_strip_recalled_memory_blocks(str(body.get("input") or "")))
    for msg in reversed(_body_messages(body)):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        if _is_responses_tool_output_type(msg.get("type")):
            continue
        content = msg.get("content") if "content" in msg else msg.get("input")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and (
                    item.get("type") == "tool_result" or _is_responses_tool_history_type(item.get("type"))
                ):
                    continue
                text = _text_from_non_tool_content([item] if isinstance(item, dict) else item)
                text = _strip_client_injected_context(_strip_recalled_memory_blocks(text))
                if text and not _looks_like_client_injected_text(text):
                    parts.append(text)
            if parts:
                return "\n".join(parts).strip()
            continue
        text = _text_from_non_tool_content(content)
        text = _strip_client_injected_context(_strip_recalled_memory_blocks(text))
        if text and not _looks_like_client_injected_text(text):
            return text
    fallback = _strip_client_injected_context(_strip_recalled_memory_blocks(_last_user_text(path, body) or ""))
    return "" if _looks_like_client_injected_text(fallback) else fallback


def _sanitize_content_for_agent_synthesis(content: Any) -> Any:
    """Return content safe for chat-only upstream final synthesis.

    Intent parsing already ignores client/runtime injected blocks.  The final
    upstream synthesizer must see the same cleaned user text; otherwise stale
    PreToolUse/SessionStart/AGENTS context can make it answer about another
    workspace or claim it has no tools even though the outer Planner owns them.
    """
    if isinstance(content, str):
        cleaned = _strip_client_injected_context(_strip_recalled_memory_blocks(content))
        return cleaned if cleaned or _looks_like_client_injected_text(content) or "[Gateway recalled memory]" in content else content
    if isinstance(content, list):
        cleaned_items: list[Any] = []
        for item in content:
            if isinstance(item, str):
                cleaned = _strip_client_injected_context(_strip_recalled_memory_blocks(item))
                if cleaned:
                    cleaned_items.append(cleaned)
                elif not (_looks_like_client_injected_text(item) or "[Gateway recalled memory]" in item):
                    cleaned_items.append(item)
                continue
            if isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
                raw = str(item.get("text") or "")
                cleaned = _strip_client_injected_context(_strip_recalled_memory_blocks(raw))
                if cleaned:
                    new_item = dict(item)
                    new_item["text"] = cleaned
                    cleaned_items.append(new_item)
                elif not (_looks_like_client_injected_text(raw) or "[Gateway recalled memory]" in raw):
                    cleaned_items.append(item)
                continue
            cleaned_items.append(item)
        return cleaned_items
    return content


def _sanitize_messages_for_agent_synthesis(messages: list[Any]) -> list[Any]:
    sanitized: list[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            sanitized.append(msg)
            continue
        if msg.get("role") in {"tool", "function"}:
            new_msg = dict(msg)
            if "content" in new_msg:
                new_msg["content"], _ = _decode_tool_result_content(new_msg.get("content") or "")
            sanitized.append(new_msg)
            continue
        if _is_responses_tool_output_type(msg.get("type")):
            new_msg = dict(msg)
            key = "output" if "output" in new_msg else "content"
            if key in new_msg:
                new_msg[key], _ = _decode_tool_result_content(new_msg.get(key) or "")
            sanitized.append(new_msg)
            continue
        if msg.get("role") != "user":
            sanitized.append(msg)
            continue
        new_msg = dict(msg)
        key = "content" if "content" in new_msg else "input"
        if key in new_msg:
            new_msg[key] = _sanitize_content_for_agent_synthesis(new_msg.get(key))
        content = new_msg.get(key)
        if content == "" or content == []:
            continue
        sanitized.append(new_msg)
    return sanitized


def _planner_conversation_text(path: str, body: Json) -> str:
    # Preserve tool-result evidence via the raw conversation text, but strip
    # directly injected recalled-memory text when it appears as normal content.
    parts: list[str] = []
    messages = _body_messages(body)
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            continue
        text = _text_from_non_tool_content(msg.get("content") if "content" in msg else msg.get("input"))
        text = _strip_recalled_memory_blocks(text)
        text = _strip_client_injected_context(text)
        if text:
            parts.append(text)
    raw = _strip_client_injected_context(_strip_recalled_memory_blocks(_conversation_text(body)))
    if parts:
        return "\n".join(parts).strip()
    # When a structured messages/input list exists, an empty visible part list
    # means the content was only tool evidence or client-injected runtime
    # guidance.  Do not fall back to the raw JSON dump: it can contain hook text
    # like "run tests" and falsely trigger planner workflows.
    if messages:
        return ""
    return raw


def _declared_non_builtin_function_intent(body: Json, user_text: str) -> bool:
    builtin = {
        "skill", "read", "open", "view_file", "ls", "list", "list_files", "list_directory",
        "glob", "file_search", "find_files", "bash", "shell", "exec_command", "run_command",
        "web_search", "websearch", "web_search_preview", "search", "browser_search",
    }
    lowered = (user_text or "").lower()
    if not lowered:
        return False
    for name, desc, _schema in _declared_tool_specs(body):
        norm = _normalize_tool_name(name)
        if norm in builtin or _gateway_owned_tool_name(name):
            continue
        haystack = f"{name} {desc}".lower().replace("_", " ")
        tokens = [token for token in re.split(r"[^a-z0-9]+", haystack) if len(token) >= 4]
        if tokens and any(token in lowered for token in tokens):
            return True
    return False


def _text_is_history_followup(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    followup_tokens = (
        "继续", "继续分析", "继续吧", "接着", "接着分析", "下一步", "然后呢", "往下", "后续",
        "continue", "go on", "next step", "next", "keep going", "carry on", "proceed",
    )
    return any(token in lowered for token in followup_tokens)


def classify_planner_intent(
    path: str,
    body: Json,
    *,
    user_text: str | None = None,
    conversation_text: str | None = None,
) -> Json:
    """Classify the current user turn before deterministic tool dispatch.

    Recalled long-context memory is stripped first because it is evidence for
    synthesis, not the current instruction.  The result is persisted in planner
    state so a remote multi-user service can explain why a workflow was chosen
    without inspecting service-local logs.
    """
    current_user_text = _bounded_planner_text(
        _strip_recalled_memory_blocks(user_text if user_text is not None else _planner_user_text(path, body))
    )
    conversation = _bounded_planner_text(
        _strip_recalled_memory_blocks(
            conversation_text if conversation_text is not None else _planner_conversation_text(path, body)
        )
    )
    source_text = current_user_text.strip() or conversation.strip()
    source = "current_user_text" if current_user_text.strip() else ("conversation_text" if conversation.strip() else "empty")
    lowered = source_text.lower()
    signals: list[str] = []
    if _declared_tool_specs(body):
        signals.append("declared_tools")
    tool_evidence = extract_tool_evidence(path, body)
    if tool_evidence:
        signals.append("tool_evidence")
    history_followup = _text_is_history_followup(current_user_text)
    history_context_allowed = (not current_user_text.strip()) or bool(tool_evidence) or history_followup

    def _intent(kind: str, workflow: str, confidence: float, reason: str, *extra_signals: str) -> Json:
        merged = signals + [signal for signal in extra_signals if signal]
        return PlannerIntent(kind, workflow, confidence, reason, merged, source).to_json()

    current_project_request = text_requests_project_inspection(current_user_text)
    history_project_request = history_context_allowed and text_requests_project_inspection(conversation)
    if current_project_request or history_project_request:
        history_signal = "conversation_followup" if history_project_request and not current_project_request else ""
        if _declared_tool_specs(body):
            return _intent("project_analysis", "project_analysis", 0.92, "matched project/codebase inspection request with declared downstream tools", "project_inspection", history_signal)
        return _intent("project_analysis", "project_analysis", 0.74, "matched project/codebase inspection request but no downstream tools are declared", "project_inspection", "missing_declared_tools", history_signal)

    should_validate, validation_kind = _conversation_requests_validation(current_user_text)
    history_validate = False
    if not should_validate and history_context_allowed:
        should_validate, validation_kind = _conversation_requests_validation(conversation)
        history_validate = should_validate
    if should_validate:
        workflow_haystack = (current_user_text + "\n" + (conversation if history_context_allowed else "")).lower()
        workflow = "fix_loop" if any(token in workflow_haystack for token in ("修复", "fix", "repair", "debug", "排障")) else "test_build"
        return _intent("validation", workflow, 0.86, f"matched validation request ({validation_kind or 'test'})", f"validation:{validation_kind or 'test'}", "conversation_followup" if history_validate else "")

    if _text_requests_code_search(current_user_text):
        return _intent("code_search", "code_search", 0.84, "matched explicit code search request", "code_search")

    edit_kind, _edit_args = _text_requests_edit_or_write(current_user_text)
    if edit_kind:
        return _intent(edit_kind, "edit", 0.88, f"matched bounded {edit_kind} request", f"{edit_kind}_request")

    skill_action, _skill_name = _extract_explicit_skill_request(current_user_text)
    if skill_action:
        return _intent("skill_request", "generic_tool", 0.82, "matched explicit skill request", "skill_request")

    if _extract_explicit_shell_command(current_user_text):
        return _intent("shell_command", "generic_tool", 0.86, "matched explicit shell/command request", "shell_command")

    paths = _extract_paths(current_user_text)
    read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开", "分析"))
    list_intent = any(token in lowered for token in ("current directory", "list files", "list directory", "当前目录", "列出文件", "目录下", "ls "))
    if paths and read_intent:
        return _intent("read_file", "generic_tool", 0.82, "matched path plus read/analyze intent", "path", "read_intent")
    if list_intent:
        return _intent("list_directory", "generic_tool", 0.8, "matched directory listing intent", "list_intent")

    test_or_build, kind = _text_requests_test_or_build(current_user_text)
    if test_or_build:
        return _intent("validation", "test_build", 0.84, f"matched explicit {kind} request", f"validation:{kind}")

    if _text_requests_web_search(current_user_text):
        return _intent("web_search", "generic_tool", 0.78, "matched web search request", "web_search")

    if _declared_non_builtin_function_intent(body, current_user_text):
        return _intent("custom_function", "generic_tool", 0.68, "matched declared custom function name/description", "custom_function")

    return _intent("plain_chat", "chat_only_synthesis", 0.5, "no planner tool intent matched current user text", "no_tool_intent")


def _persist_planner_intent(session_key: str, state: Json, intent: Json) -> Json:
    if not isinstance(state, dict):
        state = {}
    state.setdefault("session_key", session_key)
    if intent.get("workflow"):
        current = str(state.get("workflow") or "")
        if not current or current in {"generic_tool", "chat_only_synthesis"} or intent.get("kind") == "plain_chat":
            state["workflow"] = str(intent.get("workflow") or current)
    state.setdefault("current_step", "intent_classification")
    state["intent"] = _bounded_intent_snapshot(intent)
    history = state.get("intent_history")
    items: list[Json] = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    entry = dict(state["intent"])
    entry["ts"] = time.time()
    items.append(entry)
    state["intent_history"] = items[-20:]
    _store().save(session_key, state)
    _record_planner_runtime_event(
        state,
        event_type="intent_classification",
        workflow=str(intent.get("workflow") or ""),
        step="intent_classification",
        summary=f"classified planner intent: {intent.get('kind') or 'unknown'}",
        metadata={"intent": state["intent"]},
    )
    return state


def _planner_anchor_text(path: str, body: Json) -> str:
    # Use the first real user request as the anonymous-session anchor.  The
    # previous fallback used the *last* user message, which is often a
    # tool_result block in multi-round agent loops; that made planner state and
    # long-context summaries drift between turns.
    if "/responses" in path and isinstance(body.get("input"), str):
        return _strip_recalled_memory_blocks(str(body.get("input") or "")).strip()
    for msg in _body_messages(body):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") not in (None, "user"):
            continue
        if _is_responses_tool_output_type(msg.get("type")):
            continue
        text = _strip_recalled_memory_blocks(
            _text_from_non_tool_content(msg.get("content") if "content" in msg else msg.get("input"))
        )
        if text and not text.startswith("<system-reminder>") and not text.startswith("[Gateway recalled memory]") and "Gateway Agent Planner evidence is below" not in text[:500]:
            return text
    return _strip_recalled_memory_blocks(_last_user_text(path, body) or _conversation_text(body)[:2000])


def _conversation_text(body: Json) -> str:
    return _json_dumps(body.get("messages") or body.get("input") or body)


def _workspace_key(body: Json | None = None) -> str:
    try:
        from . import gateway_builtin_tools as _bt
        override = _bt._WORKSPACE_ROOT_OVERRIDE.get()
        if override is not None:
            return str(pathlib.Path(override).resolve())
    except Exception:
        pass
    if isinstance(body, dict):
        try:
            from .gateway_tool_runtime import (
                _body_has_client_workspace_hint,
                _body_has_remote_identity,
                _has_non_absolute_client_workspace_hint,
                _request_workspace_root,
            )
            if (
                _body_has_client_workspace_hint(body)
                or _has_non_absolute_client_workspace_hint(body)
                or _body_has_remote_identity(body)
            ):
                return str(_request_workspace_root(body))
        except Exception:
            pass
    try:
        return str(_workspace_root())
    except Exception:
        return "workspace:unavailable"


def _is_absolute_client_workspace_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    return pathlib.Path(text).expanduser().is_absolute() or text.startswith("~")


def _text_has_absolute_workspace_hint(text: str) -> bool:
    return bool(re.search(
        r"(?:"
        r"<cwd>\s*[/~]|"
        r"Worktree:\s*[/~]|"
        r"Primary working directory:\s*[/~]|"
        r"(?:workspace[_-]?root|gateway[_-]?workspace|projectDir|project[_-]?dir|workspace[_-]?dir|cwd|working_directory)"
        r"[\"']?\s*[:=]\s*[\"']?[/~]"
        r")",
        str(text or ""),
        flags=re.I,
    ))


def _body_has_client_workspace_hint(body: Json) -> bool:
    """Return true when the request carries downstream client workspace data."""
    keys = (
        "workspace_root",
        "gateway_workspace",
        "workspace",
        "project_dir",
        "projectDir",
        "workspace_dir",
        "cwd",
        "working_directory",
        "primary_working_directory",
        "worktree",
    )
    for key in keys:
        if _is_absolute_client_workspace_value(body.get(key)):
            return True
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            if _is_absolute_client_workspace_value(metadata.get(key)):
                return True
        user_meta = metadata.get("user_id")
        if isinstance(user_meta, str) and _text_has_absolute_workspace_hint(user_meta):
            return True
    elif isinstance(metadata, str) and _text_has_absolute_workspace_hint(metadata):
        return True
    for key in ("system", "input", "messages"):
        try:
            text = json.dumps(body.get(key), ensure_ascii=False)
        except Exception:
            text = str(body.get(key) or "")
        if _text_has_absolute_workspace_hint(text):
            return True
    return False


def _downstream_declared_path_anchor(body: Json) -> pathlib.Path | None:
    if not _body_has_client_workspace_hint(body):
        return None
    try:
        return _workspace_root()
    except Exception:
        return None


def _stable_key_part(value: Any, *, max_len: int = 96) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else _json_dumps(value)
    text = str(text or "").strip()
    if not text:
        return ""
    if len(text) <= max_len and re.fullmatch(r"[A-Za-z0-9_.:@+-]+", text):
        return text
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:24]


def _tenant_key_from_body(body: Json, meta: Json | None = None) -> str:
    meta = meta if isinstance(meta, dict) else _json_object_from_maybe_string(body.get("metadata"))
    user_meta = _json_object_from_maybe_string(meta.get("user_id"))
    scoped_client_id = None
    try:
        from . import gateway_builtin_tools as _bt
        scoped_client_id = _bt._CLIENT_ID_SCOPE_OVERRIDE.get()
    except Exception:
        scoped_client_id = None
    candidates = (
        meta.get("tenant"),
        meta.get("tenant_id"),
        meta.get("account_id"),
        meta.get("organization_id"),
        user_meta.get("tenant"),
        user_meta.get("tenant_id"),
        user_meta.get("account_id"),
        user_meta.get("organization_id"),
        user_meta.get("user_id"),
        user_meta.get("user"),
        user_meta.get("email"),
        meta.get("user_id"),
        meta.get("user"),
        body.get("user"),
        scoped_client_id,
        body.get("client_id"),
    )
    for candidate in candidates:
        part = _stable_key_part(candidate)
        if part:
            return part
    return "anonymous"


def planner_session_key(path: str, body: Json) -> str:
    meta = body.get("metadata")
    meta = meta if isinstance(meta, dict) else _json_object_from_maybe_string(meta)
    tenant = _tenant_key_from_body(body, meta)
    for key in ("conversation_id", "thread_id", "session_id", "user_id", "user"):
        val = meta.get(key) or body.get(key)
        if key == "user_id":
            nested = _json_object_from_maybe_string(val)
            val = nested.get("conversation_id") or nested.get("session_id") or val
        part = _stable_key_part(val)
        if part:
            return f"{path}:{_workspace_key(body)}:tenant:{tenant}:{key}:{part}"
    seed = _planner_anchor_text(path, body)
    digest = hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:24]
    return f"{path}:{_workspace_key(body)}:tenant:{tenant}:anon:{digest}"


def _normalize_tool_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def _gateway_owned_tool_name(name: str) -> bool:
    """Return true for tools the Gateway service can execute itself.

    Kept as a lazy import helper so the planner can avoid stealing HTTP
    Actions/MCP tools from the normal gateway-owned execution path without
    introducing module import cycles at startup.
    """
    try:
        from .gateway_http_actions import _http_action_by_name
        if _http_action_by_name(name):
            return True
    except Exception:
        pass
    try:
        from .gateway_mcp import _mcp_parse_public_name
        if _mcp_parse_public_name(name):
            return True
    except Exception:
        pass
    return False


def _declared_tool_specs(body: Json) -> list[tuple[str, str, Json]]:
    specs: list[tuple[str, str, Json]] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if isinstance(tool.get("function"), dict):
            fn = tool["function"]
            name = str(fn.get("name") or "").strip()
            desc = str(fn.get("description") or tool.get("description") or "")
            schema = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        else:
            name = str(tool.get("name") or "").strip()
            desc = str(tool.get("description") or "")
            if isinstance(tool.get("input_schema"), dict):
                schema = tool["input_schema"]
            elif isinstance(tool.get("parameters"), dict):
                schema = tool["parameters"]
            else:
                schema = {}
        if name:
            specs.append((name, desc, schema))
    return specs


def _declared_name_map(body: Json) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, _desc, _schema in _declared_tool_specs(body):
        out[name] = name
        out[name.lower()] = name
        out[_normalize_tool_name(name)] = name
    return out


def _declared_tool_name(body: Json, candidates: tuple[str, ...]) -> str | None:
    declared = _declared_name_map(body)
    for candidate in candidates:
        for key in (candidate, candidate.lower(), _normalize_tool_name(candidate)):
            if key in declared:
                return declared[key]
    return None


def _declared_schema(body: Json, tool_name: str) -> Json:
    target = _normalize_tool_name(tool_name)
    for name, _desc, schema in _declared_tool_specs(body):
        if name == tool_name or name.lower() == tool_name.lower() or _normalize_tool_name(name) == target:
            return schema
    return {}


def _schema_properties(schema: Json) -> dict[str, Any]:
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    return props


def _schema_prefers_property(schema: Json, candidates: tuple[str, ...], fallback: str) -> str:
    props = _schema_properties(schema)
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    for candidate in candidates:
        if candidate in props:
            return candidate
    for candidate in candidates:
        if candidate in required:
            return candidate
    return fallback


def _adapt_args(body: Json, tool_name: str, args: Json) -> Json:
    schema = _declared_schema(body, tool_name)
    if not schema:
        return dict(args)
    adapted = dict(args)
    props = _schema_properties(schema)
    normalized = _normalize_tool_name(tool_name)
    required = _schema_required(schema)
    if "project" in props or "project" in required:
        adapted.setdefault("project", _infer_codebase_project_name(body))
    if "command" in adapted:
        prop = _schema_prefers_property(schema, ("command", "cmd"), "command")
        if prop != "command":
            adapted[prop] = adapted.pop("command")
    if "path" in adapted:
        prop = _schema_prefers_property(schema, ("path", "file_path", "cwd", "directory"), "path")
        if prop != "path":
            adapted[prop] = adapted.pop("path")
        if prop == "file_path" and isinstance(adapted.get(prop), str):
            raw = adapted[prop]
            if raw and not pathlib.Path(raw).expanduser().is_absolute():
                root = _downstream_declared_path_anchor(body)
                if root is not None:
                    try:
                        adapted[prop] = str((root / raw).resolve(strict=False))
                    except Exception:
                        adapted[prop] = raw
    if "file_path" in adapted and isinstance(adapted.get("file_path"), str):
        raw = adapted["file_path"]
        if raw and not pathlib.Path(raw).expanduser().is_absolute():
            root = _downstream_declared_path_anchor(body)
            if root is not None:
                try:
                    adapted["file_path"] = str((root / raw).resolve(strict=False))
                except Exception:
                    adapted["file_path"] = raw
    if "name" in adapted and normalized == "skill":
        prop = _schema_prefers_property(schema, ("name", "skill"), "name")
        if prop != "name":
            adapted[prop] = adapted.pop("name")
    if "pattern" in adapted:
        prop = _schema_prefers_property(schema, ("pattern", "glob", "query"), "pattern")
        if prop != "pattern":
            adapted[prop] = adapted.pop("pattern")
    if "query" in adapted:
        prop = _schema_prefers_property(schema, ("query", "q", "search_query"), "query")
        if prop != "query":
            adapted[prop] = adapted.pop("query")
    if props and schema.get("additionalProperties") is False:
        adapted = {k: v for k, v in adapted.items() if k in props}
    return adapted


_ARG_MISSING = object()


def _json_objects_from_text(text: str) -> list[Json]:
    out: list[Json] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.I | re.S):
        try:
            value = json.loads(match.group(1))
        except Exception:
            continue
        if isinstance(value, dict):
            out.append(value)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _end = decoder.raw_decode(text[match.start():])
        except Exception:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _explicit_arg_object_from_text(text: str, props: dict[str, Any]) -> Json:
    prop_names = set(props.keys())
    for obj in _json_objects_from_text(text):
        args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else obj
        if not isinstance(args, dict):
            continue
        if prop_names and not (set(args.keys()) & prop_names):
            continue
        return args
    return {}


def _schema_type(spec: Any) -> str:
    if not isinstance(spec, dict):
        return ""
    typ = spec.get("type")
    if isinstance(typ, list):
        for item in typ:
            if item != "null":
                return str(item)
        return ""
    return str(typ or "")


def _enum_value_from_text(text: str, spec: Any) -> Any:
    if not isinstance(spec, dict) or not isinstance(spec.get("enum"), list):
        return _ARG_MISSING
    lowered = text.lower()
    for item in spec["enum"]:
        item_text = str(item)
        if lowered == item_text.lower() or re.search(rf"(?<![A-Za-z0-9_]){re.escape(item_text.lower())}(?![A-Za-z0-9_])", lowered):
            return item
    return _ARG_MISSING


def _coerce_arg_value(value: Any, spec: Any) -> Any:
    if not isinstance(spec, dict):
        return value
    enum_value = _enum_value_from_text(str(value), spec)
    if enum_value is not _ARG_MISSING:
        return enum_value
    typ = _schema_type(spec)
    if typ == "integer":
        if isinstance(value, bool):
            return _ARG_MISSING
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        match = re.search(r"-?\d+", str(value))
        return int(match.group(0)) if match else _ARG_MISSING
    if typ == "number":
        if isinstance(value, bool):
            return _ARG_MISSING
        if isinstance(value, (int, float)):
            return value
        match = re.search(r"-?(?:\d+\.\d+|\d+)", str(value))
        return float(match.group(0)) if match else _ARG_MISSING
    if typ == "boolean":
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"1", "true", "yes", "y", "on", "是", "开启", "启用"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", "否", "关闭", "禁用"}:
            return False
        return _ARG_MISSING
    if typ == "array":
        item_spec = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        raw_items = value if isinstance(value, list) else [part.strip() for part in re.split(r"[,，\n]+", str(value)) if part.strip()]
        coerced = []
        for item in raw_items:
            item_value = _coerce_arg_value(item, item_spec)
            if item_value is _ARG_MISSING:
                return _ARG_MISSING
            coerced.append(item_value)
        return coerced
    if typ == "object":
        if not isinstance(value, dict):
            return _ARG_MISSING
        nested_props = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}
        if not nested_props:
            return value
        out: Json = {}
        for key, nested_spec in nested_props.items():
            if key in value:
                nested_value = _coerce_arg_value(value[key], nested_spec)
                if nested_value is not _ARG_MISSING:
                    out[str(key)] = nested_value
        if spec.get("additionalProperties") is not False:
            for key, nested_value in value.items():
                out.setdefault(str(key), nested_value)
        return out
    if typ == "string":
        return str(value)
    return value


def _labelled_value_from_text(prop: str, text: str) -> str:
    match = re.search(rf"(?:{re.escape(prop)}|{re.escape(prop.replace('_', ' '))})\s*[:=]\s*([^\n;]+)", text, flags=re.I)
    return match.group(1).strip(" ,，。.!?") if match else ""


def _infer_custom_function_args(schema: Json, user_text: str) -> Json | None:
    props = _schema_properties(schema)
    required = _schema_required(schema)
    explicit = _explicit_arg_object_from_text(user_text, props)
    if explicit and not props:
        return explicit
    args: Json = {}
    for prop, spec in props.items():
        if prop in explicit:
            value = _coerce_arg_value(explicit[prop], spec)
            if value is _ARG_MISSING:
                return None
            args[str(prop)] = value

    location_match = re.search(r"\bin\s+([A-Za-z][A-Za-z .,'-]+?)(?:\s+(?:today|now|tomorrow|this|right)|[?.!,]|$)", user_text, flags=re.I)
    number_match = re.search(r"-?(?:\d+\.\d+|\d+)", user_text)
    for prop, spec in props.items():
        if prop in args:
            continue
        enum_value = _enum_value_from_text(user_text, spec)
        if enum_value is not _ARG_MISSING:
            args[str(prop)] = enum_value
            continue
        labelled = _labelled_value_from_text(str(prop), user_text)
        if labelled:
            value = _coerce_arg_value(labelled, spec)
            if value is not _ARG_MISSING:
                args[str(prop)] = value
                continue
        lower = str(prop).lower()
        typ = _schema_type(spec)
        if lower in {"location", "city", "place", "where"} and location_match:
            args[str(prop)] = location_match.group(1).strip()
        elif typ in {"integer", "number"} and number_match:
            value = _coerce_arg_value(number_match.group(0), spec)
            if value is not _ARG_MISSING:
                args[str(prop)] = value
        elif prop in required and typ == "string":
            args[str(prop)] = user_text.strip()
        elif prop in required and typ == "boolean":
            args[str(prop)] = False
    if required and any(prop not in args or args[prop] == "" for prop in required):
        return None
    return args


def _tool_call(body: Json, candidates: tuple[str, ...], fallback: str, args: Json, step: str, raw: Json | None = None) -> ToolCall | None:
    declared = _declared_tool_name(body, candidates)
    if body.get("tools") and declared is None:
        return None
    name = declared or fallback
    adapted = _adapt_args(body, name, args)
    schema = _declared_schema(body, name)
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    if required and any(prop not in adapted for prop in required):
        return None
    meta = {"gateway_agent_planner": True, "step": step}
    if raw:
        meta.update(raw)
    return ToolCall(f"planner_{step}_{uuid.uuid4().hex}", name, adapted, meta)


def _body_mentions_available_skill(body: Json, skill_name: str) -> bool:
    return skill_name in _conversation_text(body)


def _extract_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in PATH_RE.finditer(text or ""):
        path = match.group("path").strip().rstrip(".,;:)")
        if path and path not in paths:
            paths.append(path)
    return paths


def _extract_explicit_skill_request(text: str) -> tuple[str, str]:
    lowered = (text or "").lower()
    if "skill" not in lowered and "技能" not in lowered:
        return "", ""
    patterns = (
        r"(?:read|load|use|open)\s+skill\s+([A-Za-z0-9_.:/@-]+)",
        r"skill\s*[:=]\s*([A-Za-z0-9_.:/@-]+)",
        r"(?:读取|加载|使用|打开)\s*(?:技能|skill)\s*([A-Za-z0-9_.:/@-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            return "read", match.group(1).strip()
    if "skill" in lowered or "技能" in lowered:
        return "list", ""
    return "", ""


def _extract_explicit_shell_command(text: str) -> str:
    if not text:
        return ""
    lowered = text.lower()
    if not any(token in lowered for token in ("bash", "shell", "command", "run", "execute", "terminal", "命令", "运行", "执行")):
        return ""
    patterns = (
        r"(?:bash|shell|command|run|execute|terminal)[^`'\"]{0,120}`([^`\n]+)`",
        r"`([^`\n]+)`[^`]{0,120}(?:bash|shell|command|run|execute|terminal)",
        r"(?:命令|运行|执行)[^`'\"“”]{0,120}[`'\"“”]([^`'\"“”\n]+)[`'\"“”]",
        r"[`'\"“”]([^`'\"“”\n]+)[`'\"“”][^`'\"“”]{0,120}(?:命令|运行|执行)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def _text_requests_web_search(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in ("联网", "搜索", "查一下", "web search", "search the web", "google", "latest", "最新"))


def _extract_web_search_query(text: str) -> str:
    cleaned = re.sub(r"请|帮我|联网|搜索|查一下|web search|search the web|google", " ", text or "", flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:,.，。")
    return cleaned or (text or "").strip()


def _read_shell_command(path_value: str) -> str:
    return f"sed -n '1,240p' {shlex.quote(path_value)}"


def _shell_tool_call(body: Json, command: str, step: str, raw: Json | None = None) -> ToolCall | None:
    return _tool_call(
        body,
        ("Bash", "bash", "Shell", "shell", "exec_command", "run_command"),
        "Bash",
        {"command": command},
        step,
        raw,
    )


def _codebase_project_name_from_path(root: pathlib.Path) -> str:
    parts = [part for part in root.parts if part and part != os.sep]
    if parts:
        return "-".join(parts)
    return root.name or "default"


def _infer_codebase_project_name(body: Json | None = None) -> str:
    explicit = os.environ.get("GATEWAY_CODEBASE_MEMORY_PROJECT") or os.environ.get("CODEBASE_MEMORY_PROJECT")
    if explicit and explicit.strip():
        return explicit.strip()
    if isinstance(body, dict):
        try:
            from .gateway_tool_runtime import (
                _body_has_client_workspace_hint,
                _body_has_remote_identity,
                _has_non_absolute_client_workspace_hint,
                _logical_client_workspace_identifier,
                _request_workspace_root,
            )
            logical_workspace = _logical_client_workspace_identifier(body)
            if logical_workspace and _has_non_absolute_client_workspace_hint(body):
                project = re.sub(r"[^A-Za-z0-9_.:@+-]+", "-", logical_workspace).strip("-")
                return project or _stable_key_part(logical_workspace) or "default"
            if _body_has_client_workspace_hint(body):
                return _codebase_project_name_from_path(_request_workspace_root(body))
            if _body_has_remote_identity(body):
                return "default"
        except Exception:
            return "default"
    try:
        root = _workspace_root()
    except Exception:
        return "default"
    return _codebase_project_name_from_path(root)


def _schema_required(schema: Json) -> list[str]:
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return [str(item) for item in required]


def _extract_search_query(text: str) -> str:
    text = (text or "").strip()
    for pattern in (
        r"`([^`]{2,200})`",
        r"[\"“]([^\"”]{2,200})[\"”]",
        r"(?:搜索|查找|寻找|grep|find|search|locate)\s+(?:代码|函数|类|文件|symbol|function|class|code|for)?\s*[:：]?\s*([A-Za-z0-9_./:@*?-]{2,200})",
    ):
        match = re.search(pattern, text, flags=re.I)
        if match and match.group(1).strip():
            return match.group(1).strip()
    cleaned = re.sub(r"请|帮我|搜索|查找|寻找|代码|函数|类|文件|grep|find|search|locate|for|function|class|code|symbol", " ", text, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:,.，。")
    return cleaned or text


def _text_requests_code_search(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in (
        "搜索代码", "查找代码", "查找函数", "搜索函数", "搜索类", "查找类", "grep",
        "find function", "find class", "search code", "search for", "locate symbol",
    ))


def _code_search_call(body: Json, user_text: str) -> ToolCall | None:
    query = _extract_search_query(user_text)
    graph = _tool_call(
        body,
        ("mcp__codebase_memory_mcp__search_graph", "codebase_memory_search_graph", "search_graph"),
        "search_graph",
        {"query": query},
        "code_search",
        {"workflow": "code_search", "preferred": "codebase_memory"},
    )
    if graph is not None:
        return graph
    code = _tool_call(
        body,
        ("mcp__codebase_memory_mcp__search_code", "codebase_memory_search_code", "search_code"),
        "search_code",
        {"pattern": query},
        "code_search",
        {"workflow": "code_search", "preferred": "codebase_memory"},
    )
    if code is not None:
        return code
    grep = _tool_call(
        body,
        ("Grep", "grep", "search", "ripgrep"),
        "Grep",
        {"pattern": query, "path": "."},
        "code_search",
        {"workflow": "code_search"},
    )
    if grep is not None:
        return grep
    return _shell_tool_call(
        body,
        f"grep -RIn --exclude-dir=.git --exclude-dir=.gateway_runtime {shlex.quote(query)} . | head -200",
        "code_search",
        {"workflow": "code_search", "fallback_shell_tool": True},
    )


def _text_requests_test_or_build(text: str) -> tuple[bool, str]:
    lowered = (text or "").lower()
    test_intent = any(token in lowered for token in ("运行测试", "跑测试", "执行测试", "run tests", "run test", "pytest", "go test", "npm test"))
    build_intent = any(token in lowered for token in ("构建", "编译", "build", "typecheck", "type check", "fix build"))
    if test_intent:
        return True, "test"
    if build_intent:
        return True, "build"
    return False, ""


def _auto_test_or_build_command(kind: str, user_text: str) -> str:
    lowered = (user_text or "").lower()
    if "pytest" in lowered:
        return "python3 -m pytest -q"
    if "go test" in lowered:
        return "go test ./..."
    if "npm test" in lowered:
        return "npm test"
    if kind == "build":
        return (
            "if [ -f package.json ]; then npm run build --if-present; "
            "elif [ -f go.mod ]; then go test ./...; "
            "elif [ -f pyproject.toml ] || [ -d tests ]; then python3 -m pytest -q; "
            "else echo 'Gateway Agent Planner: no known build runner found' >&2; exit 1; fi"
        )
    return (
        "if [ -f pyproject.toml ] || [ -d tests ]; then python3 -m pytest -q; "
        "elif [ -f go.mod ]; then go test ./...; "
        "elif [ -f package.json ]; then npm test; "
        "else echo 'Gateway Agent Planner: no known test runner found' >&2; exit 1; fi"
    )


def _extract_edit_request(text: str) -> tuple[str, str, str]:
    """Parse explicit, bounded edit requests into path/old/new.

    This intentionally requires a file path and quoted old/new strings.  The
    planner should not invent writes from vague "fix it" instructions; those go
    through read/search/test evidence first.
    """
    patterns = (
        r"(?:replace|change)\s+`([^`]+)`\s+(?:with|to)\s+`([^`]+)`\s+(?:in|inside)\s+(`?[^`\s]+`?)",
        r"(?:in|inside)\s+(`?[^`\s]+`?)\s+(?:replace|change)\s+`([^`]+)`\s+(?:with|to)\s+`([^`]+)`",
        r"(?:把|将)\s*(`?[^`\s]+`?)\s*(?:中的|中|里的|里面的)?\s*`([^`]+)`\s*(?:改成|替换为|换成)\s*`([^`]+)`",
    )
    for idx, pattern in enumerate(patterns):
        match = re.search(pattern, text or "", flags=re.I | re.S)
        if not match:
            continue
        if idx == 0:
            old, new, path_value = match.group(1), match.group(2), match.group(3)
        else:
            path_value, old, new = match.group(1), match.group(2), match.group(3)
        path_value = path_value.strip("`'\"“”")
        if path_value and old:
            return path_value, old, new
    return "", "", ""


def _extract_write_request(text: str) -> tuple[str, str]:
    patterns = (
        r"(?:create|write)\s+(?:file\s+)?(`?[^`\s]+`?)\s+(?:with\s+)?(?:content|text)\s*[:：]?\s*`([^`]+)`",
        r"(?:创建|写入|新建)\s*(?:文件)?\s*(`?[^`\s]+`?)\s*(?:内容|为|写入)\s*[:：]?\s*`([^`]+)`",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I | re.S)
        if match:
            return match.group(1).strip("`'\"“”"), match.group(2)
    return "", ""


def _text_requests_edit_or_write(text: str) -> tuple[str, Json]:
    edit_path, old, new = _extract_edit_request(text)
    if edit_path:
        return "edit", {"path": edit_path, "old_string": old, "new_string": new}
    write_path, content = _extract_write_request(text)
    if write_path:
        return "write", {"path": write_path, "content": content}
    return "", {}


def _evidence_looks_like_failure(evidence: list[PlannerToolEvidence]) -> bool:
    text = "\n".join(item.content for item in evidence[-4:]).lower()
    return any(token in text for token in (
        "failed", "failure", "error", "traceback", "assertionerror", "exit_code=1",
        "panic:", "exception", "失败", "错误",
    ))


def _latest_evidence_name(evidence: list[PlannerToolEvidence]) -> str:
    for item in reversed(evidence):
        if item.name:
            return _normalize_tool_name(item.name)
    return ""


def _conversation_requests_validation(text: str) -> tuple[bool, str]:
    wants, kind = _text_requests_test_or_build(text)
    if wants:
        return True, kind
    lowered = (text or "").lower()
    if any(token in lowered for token in ("修复", "修一下", "fix", "repair", "debug", "排障")):
        return True, "test"
    return False, ""


def _planner_plan_items(workflow: str, user_text: str) -> list[Json]:
    workflow = workflow or "generic_tool"
    return _workflow_plan_items(workflow, user_text)


def _planning_tool_call(body: Json, workflow: str, user_text: str, seen_tools: set[str], evidence: list[PlannerToolEvidence]) -> ToolCall | None:
    if evidence:
        return None
    if any(name in seen_tools for name in ("update_plan", "todowrite", "todo_write")):
        return None
    declared = _declared_tool_name(body, ("update_plan", "UpdatePlan", "TodoWrite", "todo_write"))
    if declared is None:
        return None
    normalized = _normalize_tool_name(declared)
    items = _planner_plan_items(workflow, user_text)
    schema = _declared_schema(body, declared)
    props = _schema_properties(schema)
    if normalized in {"todowrite", "todo_write"} or "todos" in props:
        args: Json = {
            "todos": [
                {
                    "content": str(item.get("step") or ""),
                    "status": str(item.get("status") or "pending"),
                }
                for item in items
            ]
        }
    elif "items" in props and "plan" not in props:
        args = {"items": items}
    else:
        args = {
            "plan": items,
            "explanation": f"Gateway Agent Planner workflow: {workflow}",
        }
    return _tool_call(
        body,
        ("update_plan", "UpdatePlan", "TodoWrite", "todo_write"),
        declared,
        args,
        "planner_progress",
        {"workflow": workflow, "intent": "planner_progress"},
    )


def _paths_from_failure_evidence(evidence: list[PlannerToolEvidence]) -> list[str]:
    text = "\n".join(item.content for item in evidence[-4:])
    candidates: list[str] = []
    patterns = (
        r'File "([^"]+\.(?:py|js|ts|go|rs|java|kt))", line \d+',
        r"\b((?:src|tests|app|pkg|cmd|lib)/[A-Za-z0-9_./-]+\.(?:py|js|ts|go|rs|java|kt))[:(]\d+",
        r"\b([A-Za-z0-9_./-]+\.(?:py|js|ts|go|rs|java|kt))[:(]\d+",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            path_value = match.group(1).strip()
            lowered = path_value.lower()
            if (
                path_value.startswith(("/", "../"))
                or "site-packages/" in lowered
                or "/python" in lowered
                or lowered.startswith(("lib/python", "library/frameworks/"))
                or "/.venv/" in lowered
                or "/venv/" in lowered
            ):
                continue
            if path_value not in candidates:
                candidates.append(path_value)
            if len(candidates) >= 3:
                return candidates
    return candidates


def _read_paths_seen(body: Json) -> set[str]:
    out: set[str] = set()
    for args in _assistant_tool_input_by_id(body).values():
        for key in ("file_path", "path"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                out.add(val.strip())
    return out


def _python_source_paths_from_read_evidence(evidence: list[PlannerToolEvidence]) -> list[str]:
    candidates: list[str] = []
    for item in evidence[-6:]:
        if _normalize_tool_name(item.name) not in {"read", "open", "view_file", "tool_result"}:
            continue
        text = item.content or ""
        modules: list[str] = []
        for match in re.finditer(r"^\s*(?:\d+:\s*)?from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+", text, flags=re.M):
            modules.append(match.group(1))
        for match in re.finditer(r"^\s*(?:\d+:\s*)?import\s+([A-Za-z_][A-Za-z0-9_.]*)", text, flags=re.M):
            modules.append(match.group(1))
        for module in modules:
            if module.startswith("."):
                continue
            parts = [part for part in module.split(".") if part]
            if not parts:
                continue
            # Keep the deterministic planner conservative: only infer common
            # workspace package paths, not arbitrary third-party imports.
            if parts[0] not in {"src", "app", "pkg", "lib", "cmd"}:
                continue
            path_value = "/".join(parts) + ".py"
            if path_value not in candidates:
                candidates.append(path_value)
            if len(candidates) >= 3:
                return candidates
    return candidates


def _followup_source_read_calls(body: Json, evidence: list[PlannerToolEvidence]) -> list[ToolCall]:
    if not _evidence_looks_like_failure(evidence):
        return []
    seen_paths = _read_paths_seen(body)
    calls: list[ToolCall] = []
    for path_value in _python_source_paths_from_read_evidence(evidence):
        if path_value in seen_paths:
            continue
        call = _tool_call(
            body,
            ("Read", "read", "open", "view_file"),
            "Read",
            {"path": path_value},
            "source_followup_read",
            {"workflow": "fix_loop", "source": "diagnostic_import"},
        )
        if call is None:
            call = _shell_tool_call(
                body,
                _read_shell_command(path_value),
                "source_followup_read",
                {"workflow": "fix_loop", "fallback_shell_tool": True, "source": "diagnostic_import"},
            )
        if call is not None:
            calls.append(call)
    return calls


def _diagnostic_read_calls(body: Json, evidence: list[PlannerToolEvidence]) -> list[ToolCall]:
    if not _evidence_looks_like_failure(evidence):
        return []
    calls: list[ToolCall] = []
    for path_value in _paths_from_failure_evidence(evidence):
        call = _tool_call(
            body,
            ("Read", "read", "open", "view_file"),
            "Read",
            {"path": path_value},
            "diagnostic_read",
            {"workflow": "fix_loop", "source": "failure_evidence"},
        )
        if call is None:
            call = _shell_tool_call(
                body,
                _read_shell_command(path_value),
                "diagnostic_read",
                {"workflow": "fix_loop", "fallback_shell_tool": True, "source": "failure_evidence"},
            )
        if call is not None:
            calls.append(call)
    return calls


def _custom_function_tool_call(body: Json, user_text: str) -> ToolCall | None:
    builtin = {
        "skill", "read", "open", "view_file", "ls", "list", "list_files", "list_directory",
        "glob", "file_search", "find_files", "bash", "shell", "exec_command", "run_command",
        "web_search", "websearch", "web_search_preview", "search", "browser_search",
    }
    lowered = (user_text or "").lower()
    for name, desc, schema in _declared_tool_specs(body):
        norm = _normalize_tool_name(name)
        if norm in builtin:
            continue
        # Gateway-owned extension points must stay in the normal Gateway
        # orchestration path.  If the deterministic downstream planner emits
        # them as caller-private "custom" calls first, adapter mode returns a
        # protocol-level tool request to the client and bypasses the HTTP/MCP
        # executor that is supposed to round-trip the result back to upstream.
        if _gateway_owned_tool_name(name):
            continue
        haystack = f"{name} {desc}".lower().replace("_", " ")
        name_tokens = [t for t in re.split(r"[^a-z0-9]+", haystack) if len(t) >= 4]
        if not any(token in lowered for token in name_tokens):
            continue
        args = _infer_custom_function_args(schema, user_text)
        if args is None:
            continue
        return ToolCall(
            f"planner_custom_function_{uuid.uuid4().hex}",
            name,
            _adapt_args(body, name, args),
            {"gateway_agent_planner": True, "workflow": "generic_tool", "step": "custom_function"},
        )
    return None


def _generic_intent_decision(
    path: str,
    body: Json,
    user_text: str,
    *,
    intent: Json | None = None,
    conversation_text: str | None = None,
) -> PlannerDecision | None:
    user_text = _strip_recalled_memory_blocks(user_text or "")
    session_key = planner_session_key(path, body)
    state = _store().load(session_key)
    state.setdefault("workflow", "generic_tool")
    state.setdefault("session_key", session_key)
    evidence = extract_tool_evidence(path, body)
    state = _update_state_with_evidence(state, evidence)
    seen_tools = _tool_names_seen(body)
    conversation_text = _strip_recalled_memory_blocks(conversation_text if conversation_text is not None else _planner_conversation_text(path, body))
    if not isinstance(intent, dict):
        intent = classify_planner_intent(path, body, user_text=user_text, conversation_text=conversation_text)
    intent_kind = str(intent.get("kind") or "")
    intent_workflow = str(intent.get("workflow") or "")

    should_validate, validation_kind = _conversation_requests_validation(conversation_text)
    planned_workflow = ""
    if intent_kind == "validation":
        planned_workflow = intent_workflow or ("fix_loop" if any(token in conversation_text.lower() for token in ("修复", "fix", "repair", "debug", "排障")) else "test_build")
    elif intent_kind == "code_search":
        planned_workflow = "code_search"
    elif intent_kind in {"edit", "write"}:
        planned_workflow = "edit"
    if planned_workflow:
        call = _planning_tool_call(body, planned_workflow, user_text, seen_tools, evidence)
        if call is not None:
            state["workflow"] = planned_workflow
            state["current_step"] = "planner_progress"
            _store().save(session_key, state)
            return _planner_decision([call], planned_workflow, "planner_progress", "publish planner progress before tool execution", state)

    fix_or_qa = _fix_qa_transition_decision(
        body,
        state,
        evidence,
        seen_tools,
        should_validate=should_validate,
        validation_kind=validation_kind,
        conversation_text=conversation_text,
    )
    if fix_or_qa is not None:
        return fix_or_qa

    code_search = _code_search_transition_decision(
        body,
        user_text,
        state,
        evidence,
        seen_tools,
        intent_kind=intent_kind,
    )
    if code_search is not None:
        return code_search

    test_build = _test_build_transition_decision(
        body,
        user_text,
        state,
        evidence,
        seen_tools,
        intent_kind=intent_kind,
        validation_kind=validation_kind,
    )
    if test_build is not None:
        return test_build

    edit = _edit_transition_decision(
        body,
        user_text,
        state,
        evidence,
        seen_tools,
        intent_kind=intent_kind,
    )
    if edit is not None:
        return edit

    generic_tool = _generic_tool_transition_decision(
        body,
        user_text,
        state,
        evidence,
        seen_tools,
        intent_kind=intent_kind,
    )
    if generic_tool is not None:
        return generic_tool
    return None


def text_requests_project_inspection(text: str) -> bool:
    lowered = (text or "").lower()
    if PROJECT_INTENT_RE.search(text or ""):
        return True
    return any(token in lowered for token in (
        "分析这套项目", "分析这个项目", "分析这套工程", "分析这个工程",
        "project structure", "analyze this project", "inspect this project",
        "codebase analysis", "understand this repo",
    ))


def _project_shell_command(target: str = ".") -> str:
    q = shlex.quote(target or ".")
    return (
        f"pwd; printf '\\n--- files ---\\n'; "
        f"find {q} -maxdepth 3 -type f "
        "\\( -name '*.py' -o -name '*.md' -o -name '*.json' -o -name '*.toml' "
        "-o -name '*.yaml' -o -name '*.yml' -o -name '*.go' -o -name '*.rs' "
        "-o -name '*.js' -o -name '*.ts' -o -name 'Dockerfile' \\) "
        "| sed 's#^./##' | sort | head -200; "
        "printf '\\n--- key manifests ---\\n'; "
        "for f in README.md pyproject.toml package.json go.mod Cargo.toml requirements.txt CLAUDE.md AGENTS.md; do "
        "[ -f \"$f\" ] && printf '\\n### %s\\n' \"$f\" && sed -n '1,120p' \"$f\"; done"
    )


def _project_overview_query() -> str:
    return "project architecture entrypoints routes handlers config tests README"


def _project_core_flow_query() -> str:
    return (
        "core request flow entrypoints routers handlers controllers services "
        "main run orchestration chat completions responses messages tool execution"
    )


def _workspace_structure_calls(body: Json, step: str = "project_structure") -> list[ToolCall]:
    calls: list[ToolCall] = []
    # Prefer higher-level code graph tools if the downstream explicitly exposes them.
    arch_candidates = ("mcp__codebase_memory_mcp__get_architecture", "codebase_memory_get_architecture", "get_architecture")
    if _declared_tool_name(body, arch_candidates) is not None:
        arch = _tool_call(
            body,
            arch_candidates,
            "get_architecture",
            {},
            step,
            {"workflow": "project_analysis", "preferred": "codebase_memory"},
        )
        if arch is not None:
            calls.append(arch)
            return calls

    graph_candidates = ("mcp__codebase_memory_mcp__search_graph", "codebase_memory_search_graph", "search_graph")
    if _declared_tool_name(body, graph_candidates) is not None:
        graph = _tool_call(
            body,
            graph_candidates,
            "search_graph",
            {"query": _project_overview_query()},
            step,
            {"workflow": "project_analysis", "preferred": "codebase_memory"},
        )
        if graph is not None:
            calls.append(graph)
            return calls

    code_candidates = ("mcp__codebase_memory_mcp__search_code", "codebase_memory_search_code", "search_code")
    if _declared_tool_name(body, code_candidates) is not None:
        code = _tool_call(
            body,
            code_candidates,
            "search_code",
            {"pattern": "class|def|func|route|handler|main|README|config"},
            step,
            {"workflow": "project_analysis", "preferred": "codebase_memory"},
        )
        if code is not None:
            calls.append(code)
            return calls

    ls_call = _tool_call(body, ("LS", "ls", "list", "list_files", "list_directory"), "LS", {"path": "."}, step, {"workflow": "project_analysis"})
    if ls_call is not None:
        calls.append(ls_call)
    glob_name = _declared_tool_name(body, ("Glob", "glob", "file_search", "find_files"))
    if glob_name is not None or not body.get("tools"):
        glob_name = glob_name or "Glob"
        calls.append(ToolCall(f"planner_{step}_{uuid.uuid4().hex}", glob_name, _adapt_args(body, glob_name, {"pattern": "**/*.{py,go,rs,js,ts,json,toml,yaml,yml,md}", "path": "."}), {"gateway_agent_planner": True, "workflow": "project_analysis", "step": step}))
    if calls:
        return calls
    shell = _tool_call(
        body,
        ("Bash", "bash", "Shell", "shell", "exec_command", "run_command"),
        "Bash",
        {"command": _project_shell_command(".")},
        step,
        {"workflow": "project_analysis", "fallback_shell_tool": True},
    )
    return [shell] if shell is not None else []


def _core_flow_trace_calls(body: Json, step: str = "core_flow_trace") -> list[ToolCall]:
    """Ask for the next analysis layer after coarse project structure.

    Native coding agents do not stop after listing files: they trace the core
    entrypoints/request path before synthesizing.  For chat-only upstreams the
    gateway planner owns that second evidence-gathering step.
    """
    graph_candidates = ("mcp__codebase_memory_mcp__search_graph", "codebase_memory_search_graph", "search_graph")
    if _declared_tool_name(body, graph_candidates) is not None:
        graph = _tool_call(
            body,
            graph_candidates,
            "search_graph",
            {"query": _project_core_flow_query()},
            step,
            {"workflow": "project_analysis", "preferred": "codebase_memory"},
        )
        if graph is not None:
            return [graph]

    code_candidates = ("mcp__codebase_memory_mcp__search_code", "codebase_memory_search_code", "search_code")
    if _declared_tool_name(body, code_candidates) is not None:
        code = _tool_call(
            body,
            code_candidates,
            "search_code",
            {"pattern": "route|router|handler|controller|service|main|run_tool_orchestration|chat/completions|responses|messages"},
            step,
            {"workflow": "project_analysis", "preferred": "codebase_memory"},
        )
        if code is not None:
            return [code]

    shell = _tool_call(
        body,
        ("Bash", "bash", "Shell", "shell", "exec_command", "run_command"),
        "Bash",
        {
            "command": (
                "printf '%s\\n' '--- likely entrypoints / routes ---'; "
                "grep -RIn --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules "
                "-E 'route|router|handler|controller|def main|func main|chat/completions|responses|messages|run_tool_orchestration' "
                ". 2>/dev/null | head -120"
            )
        },
        step,
        {"workflow": "project_analysis", "fallback_shell_tool": True},
    )
    return [shell] if shell is not None else []


def _qualified_names_from_evidence(evidence: list[PlannerToolEvidence], max_items: int = 4) -> list[str]:
    """Extract code graph qualified names from search_graph/search_code evidence."""
    names: list[str] = []
    for item in evidence[-6:]:
        text = item.content or ""
        for match in QUALIFIED_NAME_RE.finditer(text):
            value = match.group("name").strip()
            if (
                value
                and value not in names
                and " " not in value
                and "\n" not in value
                and len(value) < 300
            ):
                names.append(value)
                if len(names) >= max_items:
                    return names
    return names


def _function_name_from_qualified_name(value: str) -> str:
    leaf = (value or "").rstrip(".").split(".")[-1]
    return leaf or value


def _symbol_deep_dive_calls(body: Json, evidence: list[PlannerToolEvidence], step: str = "symbol_deep_dive") -> list[ToolCall]:
    """Read source/trace for symbols discovered by code graph search.

    This keeps the chat-only upstream away from guessing implementation details
    from search result snippets alone.
    """
    qnames = _qualified_names_from_evidence(evidence)
    if not qnames:
        return []

    calls: list[ToolCall] = []
    snippet_candidates = ("mcp__codebase_memory_mcp__get_code_snippet", "codebase_memory_get_code_snippet", "get_code_snippet")
    if _declared_tool_name(body, snippet_candidates) is not None:
        call = _tool_call(
            body,
            snippet_candidates,
            "get_code_snippet",
            {"qualified_name": qnames[0], "include_neighbors": True},
            step,
            {"workflow": "project_analysis", "preferred": "codebase_memory"},
        )
        if call is not None:
            calls.append(call)

    trace_candidates = ("mcp__codebase_memory_mcp__trace_path", "codebase_memory_trace_path", "trace_path")
    if _declared_tool_name(body, trace_candidates) is not None:
        call = _tool_call(
            body,
            trace_candidates,
            "trace_path",
            {
                "function_name": _function_name_from_qualified_name(qnames[0]),
                "direction": "both",
                "mode": "calls",
                "depth": 2,
            },
            step,
            {"workflow": "project_analysis", "preferred": "codebase_memory"},
        )
        if call is not None:
            calls.append(call)
    return calls


def _assistant_tool_name_by_id(body: Json) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for msg in _body_messages(body):
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type")
        if _is_responses_tool_call_type(msg_type):
            tid = str(msg.get("call_id") or msg.get("id") or "")
            if tid:
                mapping[tid] = _responses_tool_call_name(msg)
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = str(block.get("id") or "")
                    if tid:
                        mapping[tid] = str(block.get("name") or "")
                elif isinstance(block, dict) and _is_responses_tool_call_type(block.get("type")):
                    tid = str(block.get("call_id") or block.get("id") or "")
                    if tid:
                        mapping[tid] = _responses_tool_call_name(block)
        for call in msg.get("tool_calls") or []:
            if isinstance(call, dict):
                tid = str(call.get("id") or "")
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                if tid:
                    mapping[tid] = str(fn.get("name") or call.get("name") or "")
        legacy = msg.get("function_call")
        if isinstance(legacy, dict):
            name = str(legacy.get("name") or "")
            if name:
                mapping[_legacy_function_call_id(name)] = name
    return mapping


def _assistant_tool_input_by_id(body: Json) -> dict[str, Json]:
    mapping: dict[str, Json] = {}

    for msg in _body_messages(body):
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type")
        if _is_responses_tool_call_type(msg_type):
            tid = str(msg.get("call_id") or msg.get("id") or "")
            parsed = _responses_tool_call_arguments_value(msg)
            if tid and parsed:
                mapping[tid] = parsed
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = str(block.get("id") or "")
                    raw_input = block.get("input")
                    if tid and isinstance(raw_input, dict):
                        mapping[tid] = dict(raw_input)
                elif isinstance(block, dict) and _is_responses_tool_call_type(block.get("type")):
                    tid = str(block.get("call_id") or block.get("id") or "")
                    parsed = _responses_tool_call_arguments_value(block)
                    if tid and parsed:
                        mapping[tid] = parsed
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            tid = str(call.get("id") or "")
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            raw_args = fn.get("arguments") or call.get("arguments") or {}
            parsed: Json = {}
            if isinstance(raw_args, dict):
                parsed = dict(raw_args)
            elif isinstance(raw_args, str):
                try:
                    value = json.loads(raw_args)
                    if isinstance(value, dict):
                        parsed = value
                except Exception:
                    parsed = {}
            if tid and parsed:
                mapping[tid] = parsed
        legacy = msg.get("function_call")
        if isinstance(legacy, dict):
            name = str(legacy.get("name") or "")
            raw_args = legacy.get("arguments") or {}
            parsed: Json = {}
            if isinstance(raw_args, dict):
                parsed = dict(raw_args)
            elif isinstance(raw_args, str):
                try:
                    value = json.loads(raw_args)
                    if isinstance(value, dict):
                        parsed = value
                except Exception:
                    parsed = {}
            if name and parsed:
                mapping[_legacy_function_call_id(name)] = parsed
    return mapping


def extract_tool_evidence(path: str, body: Json) -> list[PlannerToolEvidence]:
    mapping = _assistant_tool_name_by_id(body)
    inputs = _assistant_tool_input_by_id(body)
    evidence: list[PlannerToolEvidence] = []
    for msg in _body_messages(body):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool":
            call_id = str(msg.get("tool_call_id") or msg.get("id") or "")
            text, is_error = _decode_tool_result_content(msg.get("content") or "")
            is_error = is_error or bool(msg.get("is_error"))
            if inputs.get(call_id):
                text = f"[tool_args:{json.dumps(inputs[call_id], ensure_ascii=False, sort_keys=True)}]\n{text}"
            evidence.append(PlannerToolEvidence(call_id, mapping.get(call_id, "tool"), text, is_error))
        if msg.get("role") == "function":
            name = str(msg.get("name") or "")
            call_id = str(msg.get("tool_call_id") or msg.get("id") or "")
            if not call_id:
                call_id = _legacy_function_call_id(name)
            text, is_error = _decode_tool_result_content(msg.get("content") or "")
            is_error = is_error or bool(msg.get("is_error"))
            if inputs.get(call_id):
                text = f"[tool_args:{json.dumps(inputs[call_id], ensure_ascii=False, sort_keys=True)}]\n{text}"
            evidence.append(PlannerToolEvidence(call_id, name or mapping.get(call_id, "function"), text, is_error))
        if _is_responses_tool_output_type(msg.get("type")):
            call_id = str(msg.get("call_id") or msg.get("tool_call_id") or msg.get("id") or "")
            text, is_error = _decode_tool_result_content(_responses_tool_output_content(msg))
            is_error = is_error or bool(msg.get("is_error")) or str(msg.get("status") or "").lower() in {"error", "failed", "incomplete"}
            if inputs.get(call_id):
                text = f"[tool_args:{json.dumps(inputs[call_id], ensure_ascii=False, sort_keys=True)}]\n{text}"
            evidence.append(PlannerToolEvidence(call_id, mapping.get(call_id, "function_call_output"), text, is_error))
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    call_id = str(block.get("tool_use_id") or block.get("id") or "")
                    raw_content = block.get("content")
                    if isinstance(raw_content, list):
                        text = "\n".join(str(x.get("text") if isinstance(x, dict) else x) for x in raw_content)
                    else:
                        text = str(raw_content or "")
                    if inputs.get(call_id):
                        text = f"[tool_args:{json.dumps(inputs[call_id], ensure_ascii=False, sort_keys=True)}]\n{text}"
                    evidence.append(PlannerToolEvidence(call_id, mapping.get(call_id, "tool_result"), text, bool(block.get("is_error"))))
                elif _is_responses_tool_output_type(block.get("type")):
                    call_id = str(block.get("call_id") or block.get("tool_call_id") or block.get("id") or "")
                    text, is_error = _decode_tool_result_content(_responses_tool_output_content(block))
                    is_error = is_error or bool(block.get("is_error")) or str(block.get("status") or "").lower() in {"error", "failed", "incomplete"}
                    if inputs.get(call_id):
                        text = f"[tool_args:{json.dumps(inputs[call_id], ensure_ascii=False, sort_keys=True)}]\n{text}"
                    evidence.append(PlannerToolEvidence(call_id, mapping.get(call_id, "function_call_output"), text, is_error))
    return evidence


def _summarize_evidence(evidence: list[PlannerToolEvidence], max_chars: int = 6000) -> str:
    parts: list[str] = []
    for item in evidence:
        content = (item.content or "").strip()
        if not content:
            continue
        content = re.sub(r"\n{3,}", "\n\n", content)
        if len(content) > 1800:
            content = content[:1600] + "\n...<truncated by gateway agent planner>...\n" + content[-200:]
        parts.append(f"[{item.name or 'tool'}:{item.call_id}]\n{content}")
        if sum(len(p) for p in parts) > max_chars:
            break
    summary = "\n\n".join(parts)
    if len(summary) > max_chars:
        return summary[:max_chars] + "\n...<planner evidence summary truncated>"
    return summary


def _update_state_with_evidence(state: Json, evidence: list[PlannerToolEvidence]) -> Json:
    if not evidence:
        return state
    seen = set(state.get("evidence_ids") or [])
    new_items = [e for e in evidence if e.call_id and e.call_id not in seen]
    if not new_items:
        return state
    seen.update(e.call_id for e in new_items if e.call_id)
    state["evidence_ids"] = sorted(seen)
    state["evidence_count"] = int(state.get("evidence_count") or 0) + len(new_items)
    _record_planner_runtime_event(
        state,
        event_type="tool_result",
        workflow=str(state.get("workflow") or ""),
        step=str(state.get("current_step") or ""),
        summary=f"received {len(new_items)} tool result(s)",
        metadata={
            "evidence_count": state.get("evidence_count", 0),
            "results": [
                {"call_id": item.call_id, "name": item.name, "is_error": item.is_error, "content_chars": len(item.content or "")}
                for item in new_items
            ],
        },
    )
    completed_steps = set(state.get("completed_steps") or [])
    for item in new_items:
        match = PLANNER_CALL_ID_RE.match(item.call_id or "")
        if match:
            completed_steps.add(match.group("step"))
    if completed_steps:
        state["completed_steps"] = sorted(completed_steps)
    existing = str(state.get("evidence_summary") or "")
    addition = _summarize_evidence(new_items, max_chars=4000)
    combined = (existing + "\n\n" + addition).strip() if existing else addition
    # Periodic compaction: every few tool results, first try an LLM summary,
    # then retain a bounded rolling summary if the optional LLM path is absent.
    should_compact = len(combined) > _planner_summary_trigger_chars() or state["evidence_count"] % _planner_summary_every_n() == 0
    if should_compact:
        llm_summary = _summarize_planner_evidence_via_llm(combined)
        if llm_summary:
            combined = llm_summary
            state["llm_compaction_count"] = int(state.get("llm_compaction_count") or 0) + 1
        else:
            combined = combined[-_planner_summary_max_chars():]
        state["compaction_count"] = int(state.get("compaction_count") or 0) + 1
        _record_planner_runtime_event(
            state,
            event_type="evidence_compaction",
            workflow=str(state.get("workflow") or ""),
            step=str(state.get("current_step") or ""),
            summary="planner evidence compacted",
            metadata={
                "evidence_count": state.get("evidence_count", 0),
                "compaction_count": state.get("compaction_count", 0),
                "llm_compaction_count": state.get("llm_compaction_count", 0),
                "summary_chars": len(combined),
                "used_llm": bool(llm_summary),
            },
        )
    state["evidence_summary"] = combined
    return state


def _planner_summary_every_n() -> int:
    try:
        return max(1, int(os.environ.get("GATEWAY_AGENT_PLANNER_SUMMARY_EVERY") or "4"))
    except ValueError:
        return 4


def _planner_summary_trigger_chars() -> int:
    try:
        return max(2000, int(os.environ.get("GATEWAY_AGENT_PLANNER_SUMMARY_TRIGGER_CHARS") or "9000"))
    except ValueError:
        return 9000


def _planner_summary_max_chars() -> int:
    try:
        return max(2000, int(os.environ.get("GATEWAY_AGENT_PLANNER_SUMMARY_MAX_CHARS") or "9000"))
    except ValueError:
        return 9000


def _planner_llm_summary_enabled() -> bool:
    raw = str(os.environ.get("GATEWAY_AGENT_PLANNER_LLM_SUMMARY", "auto") or "auto").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _summarize_planner_evidence_via_llm(evidence_text: str) -> str | None:
    if not evidence_text.strip() or not _planner_llm_summary_enabled():
        return None
    try:
        from .gateway_context import _summarize_via_llm
        summary = _summarize_via_llm([
            {
                "role": "user",
                "content": (
                    "Summarize this Gateway Agent Planner evidence for future tool planning. "
                    "Preserve concrete file names, commands, errors, API paths, decisions, and unresolved TODOs.\n\n"
                    + evidence_text[-12000:]
                ),
            }
        ], max_summary_tokens=900)
        if summary and summary.strip():
            return summary.strip()[:_planner_summary_max_chars()]
    except Exception:
        return None
    return None


def _has_tool_named(body: Json, name: str) -> bool:
    target = _normalize_tool_name(name)
    for msg in _body_messages(body):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and _normalize_tool_name(str(block.get("name") or "")) == target:
                    return True
        for call in msg.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                if _normalize_tool_name(str(fn.get("name") or call.get("name") or "")) == target:
                    return True
    return False


def _tool_names_seen(body: Json) -> set[str]:
    out: set[str] = set()
    for msg in _body_messages(body):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    out.add(_normalize_tool_name(str(block.get("name") or "")))
        for call in msg.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                out.add(_normalize_tool_name(str(fn.get("name") or call.get("name") or "")))
    return out


def _key_file_read_calls(body: Json, evidence: list[PlannerToolEvidence]) -> list[ToolCall]:
    if _declared_tool_name(body, ("Read", "read", "open", "view_file")) is None and body.get("tools"):
        return []
    text = "\n".join(e.content for e in evidence[-3:])
    candidates: list[str] = []
    preferred = ("README.md", "CLAUDE.md", "AGENTS.md", "pyproject.toml", "go.mod", "package.json", "src/gateway_tool_runtime.py")
    for pref in preferred:
        if pref in text and pref not in candidates:
            candidates.append(pref)
    for path in _extract_paths(text):
        if path not in candidates and any(path.endswith(ext) for ext in (".md", ".py", ".go", ".json", ".toml")):
            candidates.append(path)
        if len(candidates) >= 3:
            break
    calls: list[ToolCall] = []
    for path_value in candidates[:3]:
        call = _tool_call(body, ("Read", "read", "open", "view_file"), "Read", {"path": path_value}, "key_file_read", {"workflow": "project_analysis"})
        if call is not None:
            calls.append(call)
    return calls


PROJECT_STRUCTURE_TOOL_NAMES = {
    "bash", "shell", "exec_command", "ls", "glob",
    "get_architecture", "mcp_codebase_memory_mcp_get_architecture",
    "search_graph", "mcp_codebase_memory_mcp_search_graph",
    "search_code", "mcp_codebase_memory_mcp_search_code",
}


def _project_analysis_context(body: Json, state: Json, evidence: list[PlannerToolEvidence], seen_tools: set[str]) -> Json:
    completed_steps = set(state.get("completed_steps") or [])
    return {
        "skill_result_seen": any(_normalize_tool_name(e.name) == "skill" for e in evidence) or _has_tool_named(body, "Skill"),
        "completed_steps": completed_steps,
        "structure_seen": any(name in seen_tools for name in PROJECT_STRUCTURE_TOOL_NAMES),
        "read_seen": any(name in seen_tools for name in ("read", "open", "view_file")),
        "seen_tools": seen_tools,
    }


def _project_analysis_condition_matches(condition: str, ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
    completed_steps = ctx.get("completed_steps") if isinstance(ctx.get("completed_steps"), set) else set()
    if condition == "before_evidence_and_no_plan":
        return True
    if condition == "skill_available_before_structure":
        return (
            "skill" not in ctx.get("seen_tools", set())
            and "project_structure" not in completed_steps
            and not bool(ctx.get("structure_seen"))
        )
    if condition == "after_skill_without_structure":
        return bool(ctx.get("skill_result_seen")) and not bool(ctx.get("structure_seen"))
    if condition == "after_project_structure_without_core_flow":
        return "project_structure" in completed_steps and "core_flow_trace" not in completed_steps
    if condition == "after_core_flow_without_symbol_deep_dive":
        return "core_flow_trace" in completed_steps and "symbol_deep_dive" not in completed_steps
    if condition == "after_structure_without_read":
        return bool(ctx.get("structure_seen")) and not bool(ctx.get("read_seen"))
    if condition == "no_evidence_no_tools_fallback":
        return not evidence and not ctx.get("seen_tools")
    return False


TransitionCondition = Callable[[Json, list[PlannerToolEvidence]], bool]
TransitionBuilder = Callable[[Json, Json], list[ToolCall]]


def _workflow_transition_decision(
    workflow: str,
    transitions: list[Json],
    ctx: Json,
    condition_handlers: dict[str, TransitionCondition],
    builder_handlers: dict[str, TransitionBuilder],
) -> PlannerDecision | None:
    """Evaluate a workflow transition table and emit the first matching tool dispatch.

    This is the generic Agent Planner state-machine core.  Workflow-specific
    code supplies a context, condition handlers, and builder handlers; the
    evaluator owns ordering, state update, persistence, decision history, and
    runtime events.  Keeping this generic prevents new workflows from becoming
    another pile of gateway-specific if/else dispatch code.
    """
    state = ctx.get("state") if isinstance(ctx.get("state"), dict) else {}
    evidence = ctx.get("evidence") if isinstance(ctx.get("evidence"), list) else []
    for transition in transitions:
        if not isinstance(transition, dict):
            continue
        condition = str(transition.get("condition") or "")
        condition_fn = condition_handlers.get(condition)
        if condition_fn is None or not condition_fn(ctx, evidence):
            continue
        builder = str(transition.get("builder") or "")
        builder_fn = builder_handlers.get(builder)
        if builder_fn is None:
            continue
        step = str(transition.get("step") or "")
        calls = builder_fn(ctx, transition)
        if not calls:
            continue
        reason = str(transition.get("reason") or f"{workflow} transition {condition}")[:300]
        state["workflow"] = workflow
        state["current_step"] = step
        _store().save(str(state.get("session_key") or ""), state)
        return _planner_decision(calls, workflow, step, reason, state)
    return None


def _project_builder_planning_tool(ctx: Json, transition: Json) -> list[ToolCall]:
    del transition
    call = _planning_tool_call(
        ctx["body"],
        "project_analysis",
        str(ctx.get("user_text") or ""),
        ctx.get("seen_tools") if isinstance(ctx.get("seen_tools"), set) else set(),
        ctx.get("evidence") if isinstance(ctx.get("evidence"), list) else [],
    )
    return [call] if call is not None else []


def _project_builder_codebase_onboarding_skill(ctx: Json, transition: Json) -> list[ToolCall]:
    body = ctx["body"]
    step = str(transition.get("step") or "codebase_onboarding")
    if _declared_tool_name(body, ("Skill", "skill")) is None or not _body_mentions_available_skill(body, "codebase-onboarding"):
        return []
    call = _tool_call(
        body,
        ("Skill", "skill"),
        "Skill",
        {"name": "codebase-onboarding"},
        step,
        {"workflow": "project_analysis", "intent": "project_onboarding_skill"},
    )
    return [call] if call is not None else []


PROJECT_ANALYSIS_CONDITIONS: dict[str, TransitionCondition] = {
    "before_evidence_and_no_plan": lambda ctx, evidence: _project_analysis_condition_matches("before_evidence_and_no_plan", ctx, evidence),
    "skill_available_before_structure": lambda ctx, evidence: _project_analysis_condition_matches("skill_available_before_structure", ctx, evidence),
    "after_skill_without_structure": lambda ctx, evidence: _project_analysis_condition_matches("after_skill_without_structure", ctx, evidence),
    "after_project_structure_without_core_flow": lambda ctx, evidence: _project_analysis_condition_matches("after_project_structure_without_core_flow", ctx, evidence),
    "after_core_flow_without_symbol_deep_dive": lambda ctx, evidence: _project_analysis_condition_matches("after_core_flow_without_symbol_deep_dive", ctx, evidence),
    "after_structure_without_read": lambda ctx, evidence: _project_analysis_condition_matches("after_structure_without_read", ctx, evidence),
    "no_evidence_no_tools_fallback": lambda ctx, evidence: _project_analysis_condition_matches("no_evidence_no_tools_fallback", ctx, evidence),
}


PROJECT_ANALYSIS_BUILDERS: dict[str, TransitionBuilder] = {
    "planning_tool": _project_builder_planning_tool,
    "codebase_onboarding_skill": _project_builder_codebase_onboarding_skill,
    "workspace_structure": lambda ctx, transition: _workspace_structure_calls(ctx["body"], str(transition.get("step") or "project_structure")),
    "core_flow_trace": lambda ctx, transition: _core_flow_trace_calls(ctx["body"], str(transition.get("step") or "core_flow_trace")),
    "symbol_deep_dive": lambda ctx, transition: _symbol_deep_dive_calls(
        ctx["body"],
        ctx.get("evidence") if isinstance(ctx.get("evidence"), list) else [],
        str(transition.get("step") or "symbol_deep_dive"),
    ),
    "key_file_read": lambda ctx, transition: _key_file_read_calls(
        ctx["body"],
        ctx.get("evidence") if isinstance(ctx.get("evidence"), list) else [],
    ),
}


def _project_analysis_transition_decision(
    body: Json,
    user_text: str,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
) -> PlannerDecision | None:
    ctx = _project_analysis_context(body, state, evidence, seen_tools)
    ctx.update({
        "body": body,
        "user_text": user_text,
        "state": state,
        "evidence": evidence,
        "seen_tools": seen_tools,
    })
    return _workflow_transition_decision(
        "project_analysis",
        PROJECT_ANALYSIS_TRANSITIONS,
        ctx,
        PROJECT_ANALYSIS_CONDITIONS,
        PROJECT_ANALYSIS_BUILDERS,
    )


def _fix_qa_transition_context(
    body: Json,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
    *,
    should_validate: bool,
    validation_kind: str,
    conversation_text: str,
) -> Json:
    return {
        "body": body,
        "state": state,
        "evidence": evidence,
        "seen_tools": seen_tools,
        "should_validate": bool(should_validate),
        "validation_kind": validation_kind or "test",
        "conversation_text": conversation_text,
    }


def _qa_condition_edit_result_requires_validation(expected_kind: str) -> TransitionCondition:
    def _matches(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
        kind = str(ctx.get("validation_kind") or "test")
        if expected_kind == "test":
            kind_matches = kind != "build"
        else:
            kind_matches = kind == expected_kind
        return (
            bool(ctx.get("should_validate"))
            and kind_matches
            and _latest_evidence_name(evidence) in {"edit", "write", "multiedit", "multi_edit"}
        )
    return _matches


def _qa_builder_validate_after_edit(ctx: Json, transition: Json) -> list[ToolCall]:
    kind = str(transition.get("kind") or ctx.get("validation_kind") or "test")
    step = str(transition.get("step") or f"validate_after_{kind}")
    call = _shell_tool_call(
        ctx["body"],
        _auto_test_or_build_command(kind, str(ctx.get("conversation_text") or "")),
        step,
        {"workflow": "qa_loop", "kind": kind, "after": "edit"},
    )
    return [call] if call is not None else []


def _fix_condition_failure_without_read(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
    return bool(_diagnostic_read_calls(ctx["body"], evidence)) and not any(
        name in (ctx.get("seen_tools") if isinstance(ctx.get("seen_tools"), set) else set())
        for name in ("read", "open", "view_file")
    )


def _fix_condition_unread_source_imports(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
    return bool(_followup_source_read_calls(ctx["body"], evidence))


QA_LOOP_CONDITIONS: dict[str, TransitionCondition] = {
    "edit_result_requires_test_validation": _qa_condition_edit_result_requires_validation("test"),
    "edit_result_requires_build_validation": _qa_condition_edit_result_requires_validation("build"),
}


QA_LOOP_BUILDERS: dict[str, TransitionBuilder] = {
    "validate_after_edit": _qa_builder_validate_after_edit,
}


FIX_LOOP_CONDITIONS: dict[str, TransitionCondition] = {
    "failure_evidence_without_read": _fix_condition_failure_without_read,
    "failure_evidence_with_unread_source_imports": _fix_condition_unread_source_imports,
}


FIX_LOOP_BUILDERS: dict[str, TransitionBuilder] = {
    "diagnostic_read": lambda ctx, transition: _diagnostic_read_calls(ctx["body"], ctx.get("evidence") if isinstance(ctx.get("evidence"), list) else []),
    "source_followup_read": lambda ctx, transition: _followup_source_read_calls(ctx["body"], ctx.get("evidence") if isinstance(ctx.get("evidence"), list) else []),
}


CODE_SEARCH_TOOL_NAMES = {
    "search_graph",
    "mcp_codebase_memory_mcp_search_graph",
    "codebase_memory_search_graph",
    "search_code",
    "mcp_codebase_memory_mcp_search_code",
    "codebase_memory_search_code",
    "grep",
    "ripgrep",
    "search",
    "bash",
    "shell",
    "exec_command",
    "run_command",
}


def _code_search_condition_without_existing_search(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
    if str(ctx.get("intent_kind") or "") != "code_search":
        return False
    completed_steps = set(ctx.get("completed_steps") or [])
    if "code_search" in completed_steps:
        return False
    seen_tools = ctx.get("seen_tools") if isinstance(ctx.get("seen_tools"), set) else set()
    if any(name in CODE_SEARCH_TOOL_NAMES for name in seen_tools):
        return False
    for item in evidence:
        match = PLANNER_CALL_ID_RE.match(item.call_id or "")
        if match and match.group("step") == "code_search":
            return False
    return True


def _code_search_builder(ctx: Json, transition: Json) -> list[ToolCall]:
    del transition
    call = _code_search_call(ctx["body"], str(ctx.get("user_text") or ""))
    return [call] if call is not None else []


CODE_SEARCH_CONDITIONS: dict[str, TransitionCondition] = {
    "code_search_without_existing_search": _code_search_condition_without_existing_search,
}


CODE_SEARCH_BUILDERS: dict[str, TransitionBuilder] = {
    "code_search": _code_search_builder,
}


def _code_search_transition_decision(
    body: Json,
    user_text: str,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
    *,
    intent_kind: str,
) -> PlannerDecision | None:
    ctx: Json = {
        "body": body,
        "user_text": user_text,
        "state": state,
        "evidence": evidence,
        "seen_tools": seen_tools,
        "intent_kind": intent_kind,
        "completed_steps": set(state.get("completed_steps") or []),
    }
    return _workflow_transition_decision("code_search", CODE_SEARCH_TRANSITIONS, ctx, CODE_SEARCH_CONDITIONS, CODE_SEARCH_BUILDERS)


def _validation_kind_from_context(ctx: Json) -> str:
    text = str(ctx.get("user_text") or "")
    requested, kind = _text_requests_test_or_build(text)
    if requested:
        return kind or "test"
    return str(ctx.get("validation_kind") or "")


def _test_build_condition_without_existing_run(expected_kind: str) -> TransitionCondition:
    def _matches(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
        if str(ctx.get("intent_kind") or "") != "validation":
            return False
        kind = _validation_kind_from_context(ctx)
        if kind != expected_kind:
            return False
        step = f"run_{expected_kind}"
        completed_steps = set(ctx.get("completed_steps") or [])
        if step in completed_steps:
            return False
        seen_tools = ctx.get("seen_tools") if isinstance(ctx.get("seen_tools"), set) else set()
        if evidence and any(name in seen_tools for name in ("bash", "shell", "exec_command", "run_command")):
            return False
        return True

    return _matches


def _test_build_builder_run_validation(ctx: Json, transition: Json) -> list[ToolCall]:
    kind = str(transition.get("kind") or _validation_kind_from_context(ctx) or "test")
    step = str(transition.get("step") or f"run_{kind}")
    call = _shell_tool_call(
        ctx["body"],
        _auto_test_or_build_command(kind, str(ctx.get("user_text") or "")),
        step,
        {"workflow": "test_build", "kind": kind},
    )
    return [call] if call is not None else []


TEST_BUILD_CONDITIONS: dict[str, TransitionCondition] = {
    "validation_test_without_existing_run": _test_build_condition_without_existing_run("test"),
    "validation_build_without_existing_run": _test_build_condition_without_existing_run("build"),
}


TEST_BUILD_BUILDERS: dict[str, TransitionBuilder] = {
    "run_validation": _test_build_builder_run_validation,
}


def _test_build_transition_decision(
    body: Json,
    user_text: str,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
    *,
    intent_kind: str,
    validation_kind: str,
) -> PlannerDecision | None:
    ctx: Json = {
        "body": body,
        "user_text": user_text,
        "state": state,
        "evidence": evidence,
        "seen_tools": seen_tools,
        "intent_kind": intent_kind,
        "validation_kind": validation_kind,
        "completed_steps": set(state.get("completed_steps") or []),
    }
    return _workflow_transition_decision("test_build", TEST_BUILD_TRANSITIONS, ctx, TEST_BUILD_CONDITIONS, TEST_BUILD_BUILDERS)


def _generic_tool_context(
    body: Json,
    user_text: str,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
    *,
    intent_kind: str,
) -> Json:
    lowered = user_text.lower()
    paths = _extract_paths(user_text)
    skill_action, skill_name = _extract_explicit_skill_request(user_text)
    return {
        "body": body,
        "user_text": user_text,
        "state": state,
        "evidence": evidence,
        "seen_tools": seen_tools,
        "intent_kind": intent_kind,
        "completed_steps": set(state.get("completed_steps") or []),
        "skill_action": skill_action,
        "skill_name": skill_name,
        "shell_command": _extract_explicit_shell_command(user_text),
        "paths": paths,
        "read_intent": any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开", "分析")),
        "list_intent": any(token in lowered for token in ("current directory", "list files", "list directory", "当前目录", "列出文件", "目录下", "ls ")),
    }


def _generic_condition_intent_without_evidence(expected_kind: str) -> TransitionCondition:
    def _matches(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
        if evidence:
            return False
        if str(ctx.get("intent_kind") or "") != expected_kind:
            return False
        step_by_kind = {
            "skill_request": "skill_request",
            "shell_command": "shell_command",
            "read_file": "read_file",
            "list_directory": "list_directory",
            "web_search": "web_search",
            "custom_function": "custom_function",
        }
        step = step_by_kind.get(expected_kind, expected_kind)
        return step not in set(ctx.get("completed_steps") or [])

    return _matches


def _generic_condition_skill_request(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
    return _generic_condition_intent_without_evidence("skill_request")(ctx, evidence) and bool(ctx.get("skill_action"))


def _generic_condition_shell_command(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
    return _generic_condition_intent_without_evidence("shell_command")(ctx, evidence) and bool(ctx.get("shell_command"))


def _generic_condition_read_file(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
    paths = ctx.get("paths") if isinstance(ctx.get("paths"), list) else []
    return _generic_condition_intent_without_evidence("read_file")(ctx, evidence) and bool(paths) and bool(ctx.get("read_intent"))


def _generic_condition_list_directory(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
    return _generic_condition_intent_without_evidence("list_directory")(ctx, evidence) and bool(ctx.get("list_intent"))


def _generic_builder_skill_request(ctx: Json, transition: Json) -> list[ToolCall]:
    step = str(transition.get("step") or "skill_request")
    skill_name = str(ctx.get("skill_name") or "")
    call = _tool_call(
        ctx["body"],
        ("Skill", "skill"),
        "Skill",
        {"name": skill_name} if skill_name else {},
        step,
        {"workflow": "generic_tool"},
    )
    return [call] if call is not None else []


def _generic_builder_shell_command(ctx: Json, transition: Json) -> list[ToolCall]:
    step = str(transition.get("step") or "shell_command")
    call = _shell_tool_call(ctx["body"], str(ctx.get("shell_command") or ""), step, {"workflow": "generic_tool"})
    return [call] if call is not None else []


def _generic_builder_read_file(ctx: Json, transition: Json) -> list[ToolCall]:
    step = str(transition.get("step") or "read_file")
    paths = ctx.get("paths") if isinstance(ctx.get("paths"), list) else []
    target = str(paths[-1]) if paths else ""
    if not target:
        return []
    call = _tool_call(ctx["body"], ("Read", "read", "open", "view_file"), "Read", {"path": target}, step, {"workflow": "generic_tool"})
    if call is None:
        call = _shell_tool_call(ctx["body"], _read_shell_command(target), step, {"workflow": "generic_tool", "fallback_shell_tool": True})
    return [call] if call is not None else []


def _generic_builder_list_directory(ctx: Json, transition: Json) -> list[ToolCall]:
    step = str(transition.get("step") or "list_directory")
    paths = ctx.get("paths") if isinstance(ctx.get("paths"), list) else []
    target = str(paths[-1]) if paths else "."
    call = _tool_call(ctx["body"], ("LS", "ls", "list", "list_files", "list_directory"), "LS", {"path": target}, step, {"workflow": "generic_tool"})
    if call is None:
        call = _shell_tool_call(ctx["body"], f"ls -la {shlex.quote(target)}", step, {"workflow": "generic_tool", "fallback_shell_tool": True})
    return [call] if call is not None else []


def _generic_builder_web_search(ctx: Json, transition: Json) -> list[ToolCall]:
    step = str(transition.get("step") or "web_search")
    call = _tool_call(
        ctx["body"],
        ("web_search", "WebSearch", "web_search_preview", "search", "google", "browser_search"),
        "WebSearch",
        {"query": _extract_web_search_query(str(ctx.get("user_text") or ""))},
        step,
        {"workflow": "generic_tool"},
    )
    return [call] if call is not None else []


def _generic_builder_custom_function(ctx: Json, transition: Json) -> list[ToolCall]:
    del transition
    call = _custom_function_tool_call(ctx["body"], str(ctx.get("user_text") or ""))
    return [call] if call is not None else []


GENERIC_TOOL_CONDITIONS: dict[str, TransitionCondition] = {
    "skill_request_without_evidence": _generic_condition_skill_request,
    "shell_command_without_evidence": _generic_condition_shell_command,
    "read_file_without_evidence": _generic_condition_read_file,
    "list_directory_without_evidence": _generic_condition_list_directory,
    "web_search_without_evidence": _generic_condition_intent_without_evidence("web_search"),
    "custom_function_without_evidence": _generic_condition_intent_without_evidence("custom_function"),
}


GENERIC_TOOL_BUILDERS: dict[str, TransitionBuilder] = {
    "skill_request": _generic_builder_skill_request,
    "shell_command": _generic_builder_shell_command,
    "read_file": _generic_builder_read_file,
    "list_directory": _generic_builder_list_directory,
    "web_search": _generic_builder_web_search,
    "custom_function": _generic_builder_custom_function,
}


def _generic_tool_transition_decision(
    body: Json,
    user_text: str,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
    *,
    intent_kind: str,
) -> PlannerDecision | None:
    ctx = _generic_tool_context(body, user_text, state, evidence, seen_tools, intent_kind=intent_kind)
    return _workflow_transition_decision("generic_tool", GENERIC_TOOL_TRANSITIONS, ctx, GENERIC_TOOL_CONDITIONS, GENERIC_TOOL_BUILDERS)


def _edit_transition_context(
    body: Json,
    user_text: str,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
    *,
    intent_kind: str,
) -> Json:
    edit_kind, edit_args = _text_requests_edit_or_write(user_text)
    return {
        "body": body,
        "user_text": user_text,
        "state": state,
        "evidence": evidence,
        "seen_tools": seen_tools,
        "intent_kind": intent_kind,
        "edit_kind": edit_kind,
        "edit_args": edit_args,
        "completed_steps": set(state.get("completed_steps") or []),
    }


def _edit_condition_without_evidence(expected_kind: str, step: str) -> TransitionCondition:
    def _matches(ctx: Json, evidence: list[PlannerToolEvidence]) -> bool:
        if evidence:
            return False
        if str(ctx.get("intent_kind") or "") != expected_kind:
            return False
        if str(ctx.get("edit_kind") or "") != expected_kind:
            return False
        return step not in set(ctx.get("completed_steps") or [])

    return _matches


def _edit_builder(ctx: Json, transition: Json) -> list[ToolCall]:
    step = str(transition.get("step") or "")
    edit_kind = str(ctx.get("edit_kind") or "")
    edit_args = ctx.get("edit_args") if isinstance(ctx.get("edit_args"), dict) else {}
    if edit_kind == "edit":
        call = _tool_call(
            ctx["body"],
            ("Edit", "edit", "Replace", "replace"),
            "Edit",
            edit_args,
            step or "edit_file",
            {"workflow": "edit"},
        )
    elif edit_kind == "write":
        call = _tool_call(
            ctx["body"],
            ("Write", "write", "create_file"),
            "Write",
            edit_args,
            step or "write_file",
            {"workflow": "edit"},
        )
    else:
        call = None
    return [call] if call is not None else []


EDIT_CONDITIONS: dict[str, TransitionCondition] = {
    "bounded_edit_without_evidence": _edit_condition_without_evidence("edit", "edit_file"),
    "bounded_write_without_evidence": _edit_condition_without_evidence("write", "write_file"),
}


EDIT_BUILDERS: dict[str, TransitionBuilder] = {
    "edit_file": _edit_builder,
    "write_file": _edit_builder,
}


def _edit_transition_decision(
    body: Json,
    user_text: str,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
    *,
    intent_kind: str,
) -> PlannerDecision | None:
    ctx = _edit_transition_context(body, user_text, state, evidence, seen_tools, intent_kind=intent_kind)
    return _workflow_transition_decision("edit", EDIT_TRANSITIONS, ctx, EDIT_CONDITIONS, EDIT_BUILDERS)


def _fix_qa_transition_decision(
    body: Json,
    state: Json,
    evidence: list[PlannerToolEvidence],
    seen_tools: set[str],
    *,
    should_validate: bool,
    validation_kind: str,
    conversation_text: str,
) -> PlannerDecision | None:
    ctx = _fix_qa_transition_context(
        body,
        state,
        evidence,
        seen_tools,
        should_validate=should_validate,
        validation_kind=validation_kind,
        conversation_text=conversation_text,
    )
    qa = _workflow_transition_decision("qa_loop", QA_LOOP_TRANSITIONS, ctx, QA_LOOP_CONDITIONS, QA_LOOP_BUILDERS)
    if qa is not None:
        return qa
    return _workflow_transition_decision("fix_loop", FIX_LOOP_TRANSITIONS, ctx, FIX_LOOP_CONDITIONS, FIX_LOOP_BUILDERS)


def plan_downstream_tool_request(path: str, body: Json) -> PlannerDecision | None:
    """Return the next downstream tool step for a chat-only-upstream workflow.

    The planner deliberately avoids executing user-machine tools in the gateway.
    It only emits protocol-level tool requests that the downstream client can
    execute in its native environment.
    """
    conversation = _planner_conversation_text(path, body)
    user_text = _planner_user_text(path, body)
    session_key = planner_session_key(path, body)
    state = _store().load(session_key)
    state.setdefault("session_key", session_key)
    intent = classify_planner_intent(path, body, user_text=user_text, conversation_text=conversation)
    _persist_planner_intent(session_key, state, intent)
    generic = _generic_intent_decision(path, body, user_text, intent=intent, conversation_text=conversation)
    if generic is not None:
        return generic
    if str(intent.get("kind") or "") != "project_analysis":
        return None
    # Project-analysis planning emits downstream client tools (Skill, code
    # graph, Read, Bash, ...).  Without a declared tool surface there is
    # nowhere valid for those planner-specific calls to execute.  Leave
    # no-tools requests to the bounded direct fallback/context fanout paths;
    # those paths still attach planner metadata when they surface a generic
    # LS/Glob fanout.
    if not _declared_tool_specs(body):
        return None

    state = _store().load(session_key)
    state.setdefault("workflow", "project_analysis")
    state.setdefault("session_key", session_key)
    evidence = extract_tool_evidence(path, body)
    state = _update_state_with_evidence(state, evidence)
    seen_tools = _tool_names_seen(body)

    transition = _project_analysis_transition_decision(body, user_text, state, evidence, seen_tools)
    if transition is not None:
        return transition

    state["current_step"] = "synthesis"
    _store().save(session_key, state)
    return None


def prepare_upstream_body(path: str, body: Json, *, max_summary_chars: int = 6000) -> Json:
    """Inject the Agent Planner envelope before the chat-only upstream is called.

    This function is intentionally not limited to tool/project-analysis turns.
    In remote Agent Runtime mode every user communication must have a
    Gateway-owned planner classification, state snapshot, runtime event, and
    upstream boundary.  Plain chat is still plain chat, but it is classified as
    ``plain_chat -> chat_only_synthesis`` by the outer planner before the
    upstream model is allowed to synthesize text.
    """
    conversation = _planner_conversation_text(path, body)
    user_text = _planner_user_text(path, body)
    session_key = planner_session_key(path, body)
    state = _store().load(session_key)
    evidence = extract_tool_evidence(path, body)
    strict_every_turn = strict_agent_planner_every_turn()
    project_or_tool_context = (
        text_requests_project_inspection(conversation)
        or bool(evidence)
        or bool(state.get("evidence_summary"))
        or isinstance((body.get("gateway_context") if isinstance(body, dict) else None), dict)
        and bool((body.get("gateway_context") or {}).get("agent_planner"))
    )
    if not strict_every_turn and not project_or_tool_context:
        return body
    state.setdefault("session_key", session_key)
    intent = classify_planner_intent(path, body, user_text=user_text, conversation_text=conversation)
    state.setdefault("workflow", str(intent.get("workflow") or "chat_only_synthesis"))
    state.setdefault("current_step", "synthesis")
    state = _persist_planner_intent(session_key, state, intent)
    if not state.get("workflow"):
        state["workflow"] = str(intent.get("workflow") or "chat_only_synthesis")
    state["current_intent"] = _bounded_intent_snapshot(intent)
    state.setdefault("current_step", "synthesis")

    state = _update_state_with_evidence(state, evidence)
    _store().save(session_key, state)
    summary = str(state.get("evidence_summary") or _summarize_evidence(evidence, max_chars=max_summary_chars)).strip()
    if not summary:
        summary = (
            "No downstream tool evidence is required for this turn. "
            f"Planner intent: {intent.get('kind') or 'plain_chat'}; "
            f"workflow: {intent.get('workflow') or 'chat_only_synthesis'}."
        )
    state_snapshot = planner_state_snapshot(state, max_summary_chars=1200)
    planner_prompt = (
        "Gateway Agent Planner evidence/envelope is below. Every user communication must "
        "follow this outer planner decision. The upstream model is chat-only; "
        "do not claim you will inspect files or call tools. Use the planner "
        "intent/state/evidence below and produce the final user-facing synthesis. "
        "Do not emit JSON tool requests, function calls, or tool-use markup; "
        "the outer Gateway Agent Planner owns all tool scheduling.\n\n"
        f"Planner intent: {json.dumps(intent, ensure_ascii=False)}\n"
        f"Planner workflow: {intent.get('workflow') or state.get('workflow') or 'chat_only_synthesis'}\n"
        f"Planner current step: {state.get('current_step') or 'synthesis'}\n"
        f"Evidence summary:\n{summary[:max_summary_chars]}"
    )
    updated = dict(body)

    gateway_ctx = dict(updated.get("gateway_context") or {}) if isinstance(updated.get("gateway_context"), dict) else {}
    existing_planner_ctx = gateway_ctx.get("agent_planner") if isinstance(gateway_ctx.get("agent_planner"), dict) else None
    if existing_planner_ctx is None:
        gateway_ctx["agent_planner"] = {
            "workflow": intent.get("workflow") or state.get("workflow") or "chat_only_synthesis",
            "step": state.get("current_step") or "synthesis",
            "reason": "final synthesis from planner intent/evidence",
            "session_key": state.get("session_key"),
            "intent": _bounded_intent_snapshot(intent),
            "evidence_count": state.get("evidence_count", 0),
            "state": state_snapshot,
        }
    elif "intent" not in existing_planner_ctx:
        existing_planner_ctx["intent"] = _bounded_intent_snapshot(intent)
    gateway_ctx["planner_evidence_chars"] = len(summary)
    gateway_ctx["strategy"] = "agent_planner_final_synthesis"
    gateway_ctx["agent_planner_strict_every_turn"] = strict_every_turn
    gateway_ctx["planner_has_evidence"] = bool(evidence or int(state.get("evidence_count") or 0))
    updated["gateway_context"] = gateway_ctx

    messages = body.get("messages")
    if isinstance(messages, list):
        new_messages = _sanitize_messages_for_agent_synthesis(list(messages))
        if isinstance(updated.get("system"), str):
            existing_system = str(updated.get("system") or "").strip()
            updated["system"] = (existing_system + "\n\n" + planner_prompt).strip() if existing_system else planner_prompt
        elif new_messages and isinstance(new_messages[0], dict) and new_messages[0].get("role") == "system":
            first = dict(new_messages[0])
            first["content"] = str(first.get("content") or "") + "\n\n" + planner_prompt
            new_messages[0] = first
        else:
            new_messages.insert(0, {"role": "system", "content": planner_prompt})
        updated["messages"] = new_messages
        updated.setdefault("gateway_agent_planner", {})["evidence_injected"] = True
        return updated
    # OpenAI Responses may use input as a list of role messages.
    inp = body.get("input")
    if isinstance(inp, list):
        new_input = [{"role": "system", "content": planner_prompt}] + _sanitize_messages_for_agent_synthesis(list(inp))
        updated["input"] = new_input
        updated.setdefault("gateway_agent_planner", {})["evidence_injected"] = True
        return updated
    if isinstance(inp, str):
        existing_instructions = str(updated.get("instructions") or "").strip()
        updated["instructions"] = (existing_instructions + "\n\n" + planner_prompt).strip() if existing_instructions else planner_prompt
        cleaned_input = _strip_client_injected_context(_strip_recalled_memory_blocks(inp))
        updated["input"] = cleaned_input if cleaned_input else inp
        updated.setdefault("gateway_agent_planner", {})["evidence_injected"] = True
        return updated
    return body


_UPSTREAM_SYNTHESIS_REFUSAL_RE = re.compile(
    r"("
    r"can't\s+(?:answer|help|assist)|cannot\s+(?:answer|help|assist)|"
    r"i\s+can't\s+answer\s+this\s+question|let'?s\s+talk\s+about\s+something\s+else|"
    r"无法回答|不能回答|没法回答|换个话题|聊点别的"
    r")",
    re.I,
)

_UPSTREAM_SYNTHESIS_SCOPE_DRIFT_RE = re.compile(
    r"("
    r"上一个\s*(?:session|会话)|previous\s+(?:session|conversation)|"
    r"正确的路径|correct\s+path|wrong\s+path|旧会话|历史会话"
    r")",
    re.I,
)

_UPSTREAM_SYNTHESIS_NONANSWER_RE = re.compile(
    r"("
    r"\blet\s+me\s+(?:first\s+)?(?:see|check|inspect|look)|"
    r"\bi'?ll\s+(?:first\s+)?(?:see|check|inspect|look)|"
    r"让我先|我先(?:看|检查|读取|分析)|先(?:看|检查|读取|分析)一下|先看看"
    r")",
    re.I,
)


def _planner_response_text(path: str, response: Json) -> str:
    from .gateway_protocol import _text_from_content

    pieces: list[str] = []

    if "content" in response:
        pieces.append(_text_from_content(response.get("content")))

    if isinstance(response.get("output_text"), str):
        pieces.append(response["output_text"])

    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        pieces.append(_text_from_content(item.get("content")))
        if isinstance(item.get("text"), str):
            pieces.append(item["text"])

    for choice in response.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if isinstance(message, dict):
            pieces.append(_text_from_content(message.get("content")))
        # Some upstream shims put text directly on a choice. Keep this as a
        # fallback so a valid synthesis is not mistaken for an empty refusal.
        if isinstance(choice.get("text"), str):
            pieces.append(choice["text"])

    return "\n".join(part.strip() for part in pieces if isinstance(part, str) and part.strip())


def _planner_synthesis_context(request_body: Json) -> Json:
    ctx = request_body.get("gateway_context") if isinstance(request_body.get("gateway_context"), dict) else {}
    if not isinstance(ctx, dict):
        return {}
    if ctx.get("strategy") != "agent_planner_final_synthesis" and not isinstance(ctx.get("agent_planner"), dict):
        return {}
    return ctx


def _planner_workspace_from_context(ctx: Json) -> str:
    agent = ctx.get("agent_planner") if isinstance(ctx.get("agent_planner"), dict) else {}
    session_key = str(agent.get("session_key") or "")
    match = re.match(r"^[^:]+:(.*?):tenant:", session_key)
    if match:
        return match.group(1)
    state = agent.get("state") if isinstance(agent.get("state"), dict) else {}
    session_key = str(state.get("session_key") or "")
    match = re.match(r"^[^:]+:(.*?):tenant:", session_key)
    return match.group(1) if match else ""


def _response_paths_outside_planner_scope(text: str, ctx: Json) -> list[str]:
    workspace = _planner_workspace_from_context(ctx)
    agent = ctx.get("agent_planner") if isinstance(ctx.get("agent_planner"), dict) else {}
    state = agent.get("state") if isinstance(agent.get("state"), dict) else {}
    evidence_preview = str(state.get("evidence_summary_preview") or "")
    allowed_paths = set(_extract_paths(evidence_preview))
    if workspace:
        allowed_paths.add(workspace)

    outside: list[str] = []
    for path_text in _extract_paths(text):
        if not path_text.startswith(("/", "~/")):
            continue
        if path_text in allowed_paths:
            continue
        if workspace and path_text.startswith(workspace.rstrip("/") + "/"):
            continue
        if path_text in evidence_preview:
            continue
        outside.append(path_text)
    return outside


def _synthesis_text_conflicts_with_planner_scope(text: str, ctx: Json) -> bool:
    if not text.strip():
        return False
    if _UPSTREAM_SYNTHESIS_SCOPE_DRIFT_RE.search(text):
        return True
    return bool(_response_paths_outside_planner_scope(text, ctx))


def _synthesis_text_is_nonanswer(text: str) -> bool:
    return bool(text.strip()) and bool(_UPSTREAM_SYNTHESIS_NONANSWER_RE.search(text))


def _deterministic_planner_synthesis(path: str, request_body: Json) -> str:
    ctx = _planner_synthesis_context(request_body)
    agent = ctx.get("agent_planner") if isinstance(ctx.get("agent_planner"), dict) else {}
    state = agent.get("state") if isinstance(agent.get("state"), dict) else {}
    workflow = str(agent.get("workflow") or state.get("workflow") or "project_analysis")
    step = str(agent.get("step") or state.get("current_step") or "synthesis")
    evidence_count = agent.get("evidence_count", state.get("evidence_count", 0))
    preview = str(state.get("evidence_summary_preview") or "").strip()
    if not preview:
        session_key = planner_session_key(path, request_body)
        stored = _store().load(session_key)
        preview = str(stored.get("evidence_summary") or "").strip()[:1200]
        evidence_count = evidence_count or stored.get("evidence_count", 0)
        workflow = workflow or str(stored.get("workflow") or "project_analysis")
        step = step or str(stored.get("current_step") or "synthesis")
    if not preview:
        preview = "当前 planner 没有拿到可用的下游工具证据。"

    return (
        "Agent Planner 已接管这次请求，但 chat-only 上游没有按 planner 证据完成可靠最终回答；"
        "Gateway 已改用 planner 证据生成兜底结论。\n\n"
        f"- workflow: {workflow}\n"
        f"- step: {step}\n"
        f"- evidence_count: {evidence_count}\n\n"
        "当前可用证据摘要：\n"
        f"{preview[:1800]}\n\n"
        "结论：不能把上游的“无法回答/换个话题”、跨会话/跨 workspace 漂移内容，"
        "或没有工具调用的“我先看看”占位话术当作最终结果。"
        "如果证据里显示 Read/Bash 等下游工具失败，应继续让客户端工作区执行工具或明确报告工具失败原因。"
    )


def apply_synthesis_refusal_fallback(path: str, request_body: Json, response: Json) -> Json:
    """Replace chat-only upstream refusal text with deterministic planner synthesis.

    The upstream model is not the agent planner.  During the final synthesis
    boundary it may still ignore the injected evidence and emit a generic
    refusal such as "I can't answer; let's talk about something else".  That is
    not an acceptable agent-runtime result, because the Gateway already owns
    workflow state and evidence.  Keep successful upstream synthesis unchanged,
    but never leak a generic refusal for planner-owned final turns.
    """
    ctx = _planner_synthesis_context(request_body)
    if not ctx:
        return response
    text = _planner_response_text(path, response).strip()
    scope_drift = _synthesis_text_conflicts_with_planner_scope(text, ctx)
    nonanswer = _synthesis_text_is_nonanswer(text)
    refusal = bool(_UPSTREAM_SYNTHESIS_REFUSAL_RE.search(text))
    if text and not refusal and not scope_drift and not nonanswer:
        return response

    fallback = _deterministic_planner_synthesis(path, request_body)
    updated = dict(response)
    planner_meta = updated.setdefault("gateway_agent_planner", {})
    planner_meta["synthesis_refusal_fallback"] = refusal
    planner_meta["synthesis_scope_fallback"] = scope_drift
    planner_meta["synthesis_nonanswer_fallback"] = nonanswer

    if "/messages" in path:
        updated["content"] = [{"type": "text", "text": fallback}]
        updated["stop_reason"] = updated.get("stop_reason") or "end_turn"
        return updated
    if "/responses" in path:
        updated["output"] = [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": fallback}],
        }]
        updated["output_text"] = fallback
        updated["status"] = updated.get("status") or "completed"
        return updated

    choices = list(updated.get("choices") or [])
    if choices:
        choice = dict(choices[0])
        message = dict(choice.get("message") or {})
        message["role"] = message.get("role") or "assistant"
        message["content"] = fallback
        choice["message"] = message
        choice["finish_reason"] = "stop"
        choices[0] = choice
    else:
        choices = [{"index": 0, "message": {"role": "assistant", "content": fallback}, "finish_reason": "stop"}]
    updated["choices"] = choices
    return updated
