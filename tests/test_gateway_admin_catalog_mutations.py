from __future__ import annotations

import concurrent.futures
import base64
import json
import os
import pathlib
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from src import gateway_admin_catalog_mutations as catalog


@pytest.mark.parametrize(
    "value",
    [".", "..", "../outside", "a/b", r"a\b", "/absolute", "name with spaces", ""],
)
def test_safe_admin_skill_name_rejects_unsafe_values(value: str) -> None:
    assert catalog.safe_admin_skill_name(value) == ""


def test_create_sanitizes_name_and_enforces_restrictive_modes(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    result = catalog.apply_admin_catalog_mutation(
        "/admin/skill-create",
        {"skill_name": "My useful skill", "skill_content": "# Useful\n\nComplete content.\n"},
        skills_root=root,
    )

    skill_dir = root / "My-useful-skill"
    skill_file = skill_dir / "SKILL.md"
    assert result.success is True
    assert result.redirect == "/ui#skills"
    assert skill_file.read_text(encoding="utf-8") == "# Useful\n\nComplete content."
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(skill_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(skill_file.stat().st_mode) == 0o600


def test_update_tightens_existing_directory_and_file_modes(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "demo"
    skill_dir.mkdir(parents=True, mode=0o755)
    root.chmod(0o755)
    skill_dir.chmod(0o755)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("old", encoding="utf-8")
    skill_file.chmod(0o644)

    result = catalog.apply_admin_catalog_mutation(
        "/admin/skill-create",
        {"skill_name": "demo", "skill_content": "new"},
        skills_root=root,
    )

    assert result.success is True
    assert skill_file.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(skill_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(skill_file.stat().st_mode) == 0o600


def test_create_rejects_catalog_root_symlink(tmp_path: pathlib.Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "skills"
    root.symlink_to(outside, target_is_directory=True)

    result = catalog.apply_admin_catalog_mutation(
        "/admin/skill-create",
        {"skill_name": "demo", "skill_content": "content"},
        skills_root=root,
    )

    assert result.status == 400
    assert not (outside / "demo").exists()


def test_create_rejects_skill_directory_symlink(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "demo").symlink_to(outside, target_is_directory=True)

    result = catalog.apply_admin_catalog_mutation(
        "/admin/skill-create",
        {"skill_name": "demo", "skill_content": "content"},
        skills_root=root,
    )

    assert result.status == 400
    assert not (outside / "SKILL.md").exists()


def test_create_rejects_skill_file_symlink(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "demo"
    outside = tmp_path / "outside.md"
    skill_dir.mkdir(parents=True)
    outside.write_text("outside", encoding="utf-8")
    (skill_dir / "SKILL.md").symlink_to(outside)

    result = catalog.apply_admin_catalog_mutation(
        "/admin/skill-create",
        {"skill_name": "demo", "skill_content": "content"},
        skills_root=root,
    )

    assert result.status == 400
    assert outside.read_text(encoding="utf-8") == "outside"


def test_new_skill_directory_rolls_back_when_write_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"

    def fail_write(_skill_fd: int, _content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(catalog, "_atomic_write_skill_file", fail_write)
    with pytest.raises(OSError, match="disk full"):
        catalog.apply_admin_catalog_mutation(
            "/admin/skill-create",
            {"skill_name": "demo", "skill_content": "content"},
            skills_root=root,
        )

    assert root.is_dir()
    assert not (root / "demo").exists()


def test_post_check_directory_swap_cannot_write_outside_catalog(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"
    target = root / "demo"
    outside = tmp_path / "outside"
    stolen = root / "stolen"
    target.mkdir(parents=True)
    outside.mkdir()
    real_write = catalog._atomic_write_skill_file

    def swap_then_write(skill_fd: int, content: str) -> None:
        target.rename(stolen)
        target.symlink_to(outside, target_is_directory=True)
        real_write(skill_fd, content)

    monkeypatch.setattr(catalog, "_atomic_write_skill_file", swap_then_write)
    result = catalog.apply_admin_catalog_mutation(
        "/admin/skill-create",
        {"skill_name": "demo", "skill_content": "content"},
        skills_root=root,
    )

    assert result.status == 400
    assert not (outside / "SKILL.md").exists()


def test_concurrent_same_skill_updates_are_complete_and_leave_no_temps(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    contents = [f"# version-{index}\n\n" + (str(index) * 10_000) for index in range(8)]

    def write(content: str) -> bool:
        return catalog.apply_admin_catalog_mutation(
            "/admin/skill-create",
            {"skill_name": "demo", "skill_content": content},
            skills_root=root,
        ).success

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(write, contents))

    installed = (root / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert installed in contents
    assert list((root / "demo").glob("*.tmp")) == []


def test_marketplace_install_writes_complete_skill(tmp_path: pathlib.Path) -> None:
    result = catalog.apply_admin_catalog_mutation(
        "/admin/skill-install.json",
        {"id": "reviewer"},
        skills_root=tmp_path / "skills",
        get_skill=lambda _skill_id: {"name": "Reviewer", "description": "Review carefully."},
    )

    assert result.payload == {"ok": True, "name": "reviewer"}
    assert (tmp_path / "skills" / "reviewer" / "SKILL.md").read_text(encoding="utf-8") == (
        "# Reviewer\n\nReview carefully.\n"
    )


def test_delete_is_confined_and_restores_tombstone_on_early_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"
    skill_file = root / "demo" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("content", encoding="utf-8")

    def fail_delete(_directory_fd: int) -> None:
        raise OSError("delete failed")

    monkeypatch.setattr(catalog, "_remove_directory_contents", fail_delete)
    with pytest.raises(OSError, match="delete failed"):
        catalog.apply_admin_catalog_mutation(
            "/admin/skill-delete.json",
            {"name": "demo"},
            skills_root=root,
        )

    assert skill_file.read_text(encoding="utf-8") == "content"
    assert list(root.glob(".demo.delete-*")) == []


def test_delete_success_missing_and_symlink_rejection(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    skill_file = root / "demo" / "nested" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("content", encoding="utf-8")

    deleted = catalog.apply_admin_catalog_mutation(
        "/admin/skill-delete.json",
        {"name": "demo"},
        skills_root=root,
    )
    missing = catalog.apply_admin_catalog_mutation(
        "/admin/skill-delete.json",
        {"name": "demo"},
        skills_root=root,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    linked = catalog.apply_admin_catalog_mutation(
        "/admin/skill-delete.json",
        {"name": "linked"},
        skills_root=root,
    )

    assert deleted.payload == {"ok": True}
    assert missing.status == 404
    assert linked.status == 400
    assert (outside / "keep").read_text(encoding="utf-8") == "safe"


def _patch_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
    config: dict,
) -> tuple[list[tuple[dict, str | None]], list[str]]:
    from src import gateway_config

    saved: list[tuple[dict, str | None]] = []
    reloads: list[str] = []
    monkeypatch.setattr(gateway_config, "load_config_with_revision", lambda: (config, "revision-1"))
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda candidate, *, expected_revision=None: saved.append((candidate, expected_revision)) or "revision-2",
    )
    return saved, reloads


def test_mcp_marketplace_install_saves_argv_then_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    original = {"mcp": {"servers": []}}
    saved, reloads = _patch_mcp_config(monkeypatch, original)

    result = catalog.apply_admin_catalog_mutation(
        "/admin/mcp-install.json",
        {"id": "github"},
        get_mcp_server=lambda _mcp_id: {"package": "@modelcontextprotocol/server-github"},
        reload_mcp=lambda: reloads.append("reload"),
    )

    assert result.payload == {
        "ok": True,
        "name": "github",
        "persisted": True,
        "runtime_reloaded": True,
    }
    assert original == {"mcp": {"servers": []}}
    assert saved[0][1] == "revision-1"
    assert saved[0][0]["mcp"]["servers"] == [
        {
            "name": "github",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "enabled": True,
        }
    ]
    assert reloads == ["reload"]


def test_mcp_reload_failure_reports_persisted_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, _reloads = _patch_mcp_config(monkeypatch, {"mcp": {"servers": []}})

    def fail_reload() -> None:
        raise RuntimeError("session close failed")

    result = catalog.apply_admin_catalog_mutation(
        "/admin/mcp-install.json",
        {"id": "github"},
        get_mcp_server=lambda _mcp_id: {"package": "@modelcontextprotocol/server-github"},
        reload_mcp=fail_reload,
    )

    assert result.success is True
    assert result.payload["persisted"] is True
    assert result.payload["runtime_reloaded"] is False
    assert "session close failed" in result.payload["warning"]
    assert len(saved) == 1


@pytest.mark.parametrize(
    "package",
    ["", "--registry=https://evil.test", "../local", "https://evil.test/pkg.tgz", "pkg name", "pkg;echo"],
)
def test_mcp_marketplace_rejects_unsafe_package(package: str, monkeypatch: pytest.MonkeyPatch) -> None:
    saved, reloads = _patch_mcp_config(monkeypatch, {"mcp": {"servers": []}})
    result = catalog.apply_admin_catalog_mutation(
        "/admin/mcp-install.json",
        {"id": "unsafe"},
        get_mcp_server=lambda _mcp_id: {"package": package},
        reload_mcp=lambda: reloads.append("reload"),
    )

    assert result.status == 400
    assert saved == []
    assert reloads == []


@pytest.mark.parametrize("config", [{"mcp": []}, {"mcp": {"servers": {}}}, {"mcp": {"servers": ["bad"]}}])
def test_mcp_marketplace_rejects_malformed_configuration(
    config: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved, reloads = _patch_mcp_config(monkeypatch, config)
    result = catalog.apply_admin_catalog_mutation(
        "/admin/mcp-install.json",
        {"id": "github"},
        get_mcp_server=lambda _mcp_id: {"package": "@modelcontextprotocol/server-github"},
        reload_mcp=lambda: reloads.append("reload"),
    )

    assert result.status == 400
    assert saved == []
    assert reloads == []


def test_mcp_already_installed_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, reloads = _patch_mcp_config(
        monkeypatch,
        {"mcp": {"servers": [{"name": "github", "command": "npx", "enabled": True}]}},
    )
    result = catalog.apply_admin_catalog_mutation(
        "/admin/mcp-install.json",
        {"id": "github"},
        get_mcp_server=lambda _mcp_id: {"package": "@modelcontextprotocol/server-github"},
        reload_mcp=lambda: reloads.append("reload"),
    )

    assert result.payload == {"ok": True, "message": "already installed"}
    assert saved == []
    assert reloads == []


def test_admin_catalog_is_visible_after_project_skills(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.gateway_builtin_tools import _skill_dirs

    workspace = tmp_path / "workspace"
    project_skills = workspace / "skills"
    admin_skills = tmp_path / "admin-skills"
    project_skills.mkdir(parents=True)
    admin_skills.mkdir()
    monkeypatch.setenv("GATEWAY_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("GATEWAY_ADMIN_SKILLS_ROOT", str(admin_skills))

    discovered = _skill_dirs()

    assert project_skills.resolve() in discovered
    assert admin_skills.resolve() in discovered
    assert discovered.index(project_skills.resolve()) < discovered.index(admin_skills.resolve())


def test_admin_skills_root_environment_override(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "service-catalog"
    monkeypatch.setenv("GATEWAY_ADMIN_SKILLS_ROOT", str(configured))
    assert catalog.admin_skills_root() == configured.resolve(strict=False)


def test_unmatched_catalog_path_has_no_side_effects(tmp_path: pathlib.Path) -> None:
    result = catalog.apply_admin_catalog_mutation(
        "/admin/not-catalog",
        {"skill_name": "demo", "skill_content": "content"},
        skills_root=tmp_path / "skills",
    )
    assert result.matched is False
    assert not (tmp_path / "skills").exists()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def test_catalog_http_routes_preserve_auth_and_response_contracts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config
    from src.gateway_http_handler import GatewayHandler

    config_path = tmp_path / "config.json"
    skills_root = tmp_path / "admin-skills"
    monkeypatch.setattr(gateway_config, "CONFIG_PATH", config_path)
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("GATEWAY_ADMIN_PASSWORD", "catalog-admin-password")
    monkeypatch.setenv("GATEWAY_ADMIN_SKILLS_ROOT", str(skills_root))
    gateway_config.save_config(gateway_config._default_config())

    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    token = base64.b64encode(b"admin:catalog-admin-password").decode("ascii")
    headers = {"Authorization": f"Basic {token}"}
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        create_body = urllib.parse.urlencode(
            {"skill_name": "http-created", "skill_content": "# HTTP Created\n\nComplete."}
        ).encode("utf-8")
        create_request = urllib.request.Request(
            f"{base_url}/admin/skill-create",
            data=create_body,
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as redirect:
            opener.open(create_request, timeout=5)
        assert redirect.value.code == 302
        assert redirect.value.headers["Location"] == "/ui#skills"
        assert (skills_root / "http-created" / "SKILL.md").is_file()

        install_request = urllib.request.Request(
            f"{base_url}/admin/skill-install.json",
            data=json.dumps({"id": "tdd-workflow"}).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(install_request, timeout=5) as response:
            installed = json.loads(response.read().decode("utf-8"))
        assert installed == {"ok": True, "name": "tdd-workflow"}
        assert (skills_root / "tdd-workflow" / "SKILL.md").is_file()

        delete_request = urllib.request.Request(
            f"{base_url}/admin/skill-delete.json",
            data=json.dumps({"name": "http-created"}).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(delete_request, timeout=5) as response:
            deleted = json.loads(response.read().decode("utf-8"))
        assert deleted == {"ok": True}
        assert not (skills_root / "http-created").exists()

        mcp_request = urllib.request.Request(
            f"{base_url}/admin/mcp-install.json",
            data=json.dumps({"id": "github"}).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(mcp_request, timeout=5) as response:
            mcp_installed = json.loads(response.read().decode("utf-8"))
        assert mcp_installed == {
            "ok": True,
            "name": "github",
            "persisted": True,
            "runtime_reloaded": True,
        }
        assert gateway_config.load_config()["mcp"]["servers"][-1] == {
            "name": "github",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "enabled": True,
        }

        unauthenticated = urllib.request.Request(
            f"{base_url}/admin/skill-delete.json",
            data=json.dumps({"name": "tdd-workflow"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(unauthenticated, timeout=5)
        assert unauthorized.value.code == 401
        assert (skills_root / "tdd-workflow").is_dir()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
