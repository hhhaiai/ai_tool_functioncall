"""
MCP Server Marketplace and Skills Catalog

Provides a catalog of known MCP servers and skills that can be
automatically discovered and installed by the Gateway.
"""

import pathlib
from dataclasses import dataclass
from typing import Any


# Known MCP servers that can be auto-installed
KNOWN_MCP_SERVERS: list[dict[str, Any]] = [
    {
        "id": "github",
        "name": "GitHub",
        "description": "Interact with GitHub repositories, issues, pull requests",
        "package": "@modelcontextprotocol/server-github",
        "env_vars": ["GITHUB_TOKEN"],
        "categories": ["development", "version-control"],
    },
    {
        "id": "filesystem",
        "name": "Filesystem",
        "description": "Read, write, and manage files and directories",
        "package": "@modelcontextprotocol/server-filesystem",
        "env_vars": [],
        "categories": ["filesystem", "development"],
    },
    {
        "id": "postgres",
        "name": "PostgreSQL",
        "description": "Query and manage PostgreSQL databases",
        "package": "@modelcontextprotocol/server-postgres",
        "env_vars": ["PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"],
        "categories": ["database"],
    },
    {
        "id": "brave-search",
        "name": "Brave Search",
        "description": "Web search using Brave Search API",
        "package": "@modelcontextprotocol/server-brave-search",
        "env_vars": ["BRAVE_API_KEY"],
        "categories": ["search", "web"],
    },
    {
        "id": "slack",
        "name": "Slack",
        "description": "Send messages and manage Slack channels",
        "package": "@modelcontextprotocol/server-slack",
        "env_vars": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
        "categories": ["communication", "messaging"],
    },
    {
        "id": "sentry",
        "name": "Sentry",
        "description": "Interact with Sentry error tracking and monitoring",
        "package": "@modelcontextprotocol/server-sentry",
        "env_vars": ["SENTRY_AUTH_TOKEN", "SENTRY_ORGANIZATION"],
        "categories": ["monitoring", "debugging"],
    },
    {
        "id": "google-maps",
        "name": "Google Maps",
        "description": "Search places, get directions, and geocode locations",
        "package": "@modelcontextprotocol/server-google-maps",
        "env_vars": ["GOOGLE_MAPS_API_KEY"],
        "categories": ["geolocation", "maps"],
    },
    {
        "id": "aws-kb-retrieval",
        "name": "AWS KB Retrieval",
        "description": "Query AWS Knowledge Base for RAG applications",
        "package": "@modelcontextprotocol/server-aws-kb-retrieval-server",
        "env_vars": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "KB_ID"],
        "categories": ["aws", "rag", "knowledge-base"],
    },
    {
        "id": "puppeteer",
        "name": "Puppeteer",
        "description": "Browser automation using Puppeteer",
        "package": "@modelcontextprotocol/server-puppeteer",
        "env_vars": [],
        "categories": ["browser", "automation", "web"],
    },
    {
        "id": "fetch",
        "name": "Fetch",
        "description": "HTTP fetch for making web requests",
        "package": "@modelcontextprotocol/server-fetch",
        "env_vars": [],
        "categories": ["web", "http", "fetch"],
    },
]


# Known skills that can be loaded
KNOWN_SKILLS: list[dict[str, Any]] = [
    {
        "id": "python-reviewer",
        "name": "Python Code Reviewer",
        "description": "Expert Python code review with focus on PEP 8, security, and performance",
        "categories": ["review", "python", "security"],
    },
    {
        "id": "tdd-workflow",
        "name": "TDD Workflow",
        "description": "Test-driven development workflow guide and helpers",
        "categories": ["tdd", "testing", "workflow"],
    },
    {
        "id": "golang-reviewer",
        "name": "Go Code Reviewer",
        "description": "Expert Go code review with focus on idioms, concurrency, and performance",
        "categories": ["review", "go", "golang", "security"],
    },
    {
        "id": "code-reviewer",
        "name": "General Code Reviewer",
        "description": "General-purpose code review for multiple languages",
        "categories": ["review", "general"],
    },
    {
        "id": "planner",
        "name": "Implementation Planner",
        "description": "Helps plan feature implementation with step-by-step breakdown",
        "categories": ["planning", "workflow"],
    },
]


@dataclass
class MarketItem:
    """Represents an item in the marketplace."""
    id: str
    name: str
    description: str
    package: str | None
    source: str  # "mcp", "skill", "openapi"
    categories: list[str]
    env_vars: list[str]
    installed: bool = False
    enabled: bool = False


def list_mcp_marketplace() -> list[dict[str, Any]]:
    """List all available MCP servers from the marketplace."""
    return [
        {
            "id": server["id"],
            "name": server["name"],
            "description": server["description"],
            "package": server["package"],
            "categories": server["categories"],
            "env_vars": server["env_vars"],
            "install_command": f"npx -y {server['package']}",
            "source": "mcp",
        }
        for server in KNOWN_MCP_SERVERS
    ]


def list_skills_catalog() -> list[dict[str, Any]]:
    """List all skills available in the skills catalog."""
    return list(KNOWN_SKILLS)


def get_mcp_server_by_id(server_id: str) -> dict[str, Any] | None:
    """Get a specific MCP server from the marketplace by ID."""
    for server in KNOWN_MCP_SERVERS:
        if server["id"] == server_id:
            return server
    return None


def get_skill_by_id(skill_id: str) -> dict[str, Any] | None:
    """Get a specific skill from the catalog by ID."""
    for skill in KNOWN_SKILLS:
        if skill["id"] == skill_id:
            return skill
    return None


def search_marketplace(query: str) -> list[dict[str, Any]]:
    """Search marketplace items by name, description, or category (MCP and skills)."""
    query_lower = query.lower()
    results = []

    # Search MCP servers
    for server in KNOWN_MCP_SERVERS:
        if (query_lower in server["name"].lower() or
            query_lower in server["description"].lower() or
            any(query_lower in cat.lower() for cat in server["categories"])):
            results.append({**server, "source": "mcp"})

    # Search skills
    for skill in KNOWN_SKILLS:
        if (query_lower in skill["name"].lower() or
            query_lower in skill["description"].lower() or
            any(query_lower in cat.lower() for cat in skill["categories"])):
            results.append({**skill, "source": "skill"})

    return results


def get_install_command(server_id: str) -> str | None:
    """Get the installation command for a marketplace MCP server."""
    server = get_mcp_server_by_id(server_id)
    if server:
        return f"npx -y {server['package']}"
    return None


def scan_local_skills() -> list[dict[str, Any]]:
    """Scan local skills directories for available skills."""
    candidates = [
        pathlib.Path.cwd() / ".codex" / "skills",
        pathlib.Path.home() / ".codex" / "skills",
        pathlib.Path.home() / ".agents" / "skills",
    ]
    skills = []
    for skill_dir in candidates:
        if skill_dir.is_dir():
            for skill_path in skill_dir.glob("*/SKILL.md"):
                name = skill_path.parent.name
                try:
                    content = skill_path.read_text(encoding="utf-8")
                    # Extract description from first line
                    description = content.split("\n")[0].strip().lstrip("#").strip()
                except Exception:
                    description = f"Local skill: {name}"
                skills.append({
                    "id": name.lower().replace("_", "-"),
                    "name": name,
                    "description": description,
                    "path": str(skill_path),
                    "source": "local",
                    "categories": ["local"],
                })
    return skills
