from __future__ import annotations

import ast
import glob
import json
import re
import shlex
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FILES = (
    [ROOT / "README.md", ROOT / "CLAUDE.md"]
    + sorted((ROOT / "docs").rglob("*.md"))
    + sorted((ROOT / "skills").rglob("*.md"))
)
LOCAL_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)")


def test_gateway_admin_has_one_authoritative_renderer() -> None:
    source = (ROOT / "src" / "gateway_admin.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    renderers = [
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_render_admin_ui"
    ]

    assert len(renderers) == 1, "gateway_admin.py must contain exactly one authoritative _render_admin_ui"


def test_root_markdown_files_are_only_project_entrypoints() -> None:
    root_markdown = {path.name for path in ROOT.glob("*.md")}

    assert root_markdown == {"CLAUDE.md", "README.md"}


def test_retired_repository_files_do_not_return() -> None:
    retired_paths = {
        "config/mcp_defaults.json",
        "docs/整理结构.txt",
        "findings.md",
        "hermes-skill-deps.sh",
        "progress.md",
        "task_plan.md",
        "tool_gateway_audit_report.md",
    }

    assert not {path for path in retired_paths if (ROOT / path).exists()}


def test_markdown_local_links_resolve() -> None:
    broken: list[str] = []
    external_prefixes = ("http://", "https://", "mailto:", "data:", "#", "/")

    for markdown_file in MARKDOWN_FILES:
        for line_number, line in enumerate(markdown_file.read_text(encoding="utf-8").splitlines(), start=1):
            for match in LOCAL_LINK.finditer(line):
                raw_target = match.group("target").strip("<>")
                if raw_target.startswith(external_prefixes):
                    continue
                relative_target = unquote(raw_target.split("#", maxsplit=1)[0])
                if not relative_target:
                    continue
                resolved = (markdown_file.parent / relative_target).resolve()
                try:
                    resolved.relative_to(ROOT)
                except ValueError:
                    continue
                if not resolved.exists():
                    broken.append(
                        f"{markdown_file.relative_to(ROOT)}:{line_number} -> {raw_target}"
                    )

    assert not broken, "broken local Markdown links:\n" + "\n".join(broken)


def test_markdown_fences_are_balanced() -> None:
    unbalanced = []

    for markdown_file in MARKDOWN_FILES:
        fences = sum(
            1
            for line in markdown_file.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith("```")
        )
        if fences % 2:
            unbalanced.append(str(markdown_file.relative_to(ROOT)))

    assert not unbalanced, "unbalanced Markdown code fences: " + ", ".join(unbalanced)


def test_running_guide_complete_config_example_is_valid_and_server_safe() -> None:
    guide = (ROOT / "docs" / "RUNNING_AND_TESTING.md").read_text(encoding="utf-8")
    section = guide.split("### 3.2 ", maxsplit=1)[1].split("### 3.3 ", maxsplit=1)[0]
    match = re.search(r"```json\n(?P<payload>.*?)\n```", section, flags=re.DOTALL)

    assert match is not None
    payload = json.loads(match.group("payload"))
    assert "workspace_root" not in payload["gateway"]
    assert set(payload["upstream"]["capabilities"]) == {
        "supports_tools",
        "supports_function_calls",
        "supports_parallel_tool_calls",
        "supports_web_search",
        "supports_image_recognition",
        "supports_music_recognition",
        "supports_video_recognition",
        "supports_audio_recognition",
        "supports_speech",
        "supports_streaming",
        "supports_json_schema",
        "supports_network",
        "supports_vision",
    }
    assert payload["upstream"]["models"] == [
        {"name": payload["upstream"]["model"], "capability_overrides": {}}
    ]


def test_all_project_docs_are_reachable_from_the_docs_index() -> None:
    docs_root = ROOT / "docs"
    pending = [docs_root / "README.md"]
    visited: set[Path] = set()

    while pending:
        markdown_file = pending.pop()
        if markdown_file in visited:
            continue
        visited.add(markdown_file)
        text = markdown_file.read_text(encoding="utf-8")
        for match in LOCAL_LINK.finditer(text):
            raw_target = match.group("target").strip("<>")
            if raw_target.startswith(("http://", "https://", "mailto:", "data:", "#", "/")):
                continue
            relative_target = unquote(raw_target.split("#", maxsplit=1)[0])
            if not relative_target:
                continue
            resolved = (markdown_file.parent / relative_target).resolve()
            if resolved.is_dir():
                resolved /= "README.md"
            if resolved.suffix == ".md" and resolved.is_relative_to(docs_root) and resolved.exists():
                pending.append(resolved)

    expected = set(docs_root.rglob("*.md"))
    unreachable = sorted(str(path.relative_to(ROOT)) for path in expected - visited)
    assert not unreachable, "docs missing from docs/README.md navigation:\n" + "\n".join(unreachable)


def test_dockerfile_copy_sources_exist() -> None:
    missing: list[str] = []

    for line_number, raw_line in enumerate(
        (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        tokens = [token for token in shlex.split(line[5:]) if not token.startswith("--")]
        for source in tokens[:-1]:
            matches = glob.glob(str(ROOT / source))
            if not matches:
                missing.append(f"Dockerfile:{line_number} -> {source}")

    assert not missing, "Docker COPY sources missing from the build context:\n" + "\n".join(missing)
