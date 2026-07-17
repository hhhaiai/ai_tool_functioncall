"""Safe Admin mutations for local Skills and Marketplace MCP entries."""
from __future__ import annotations

import copy
import os
import pathlib
import re
import stat
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .gateway_file_ops import fsync_directory, path_write_lock

Json = dict[str, Any]
_PATHS = {
    "/admin/skill-create",
    "/admin/skill-install.json",
    "/admin/skill-delete.json",
    "/admin/mcp-install.json",
}
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_NPM_PACKAGE_RE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9._+-]*)?$"
)
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
_FILE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))


@dataclass(frozen=True)
class AdminCatalogMutationResult:
    matched: bool
    success: bool = False
    status: int = 0
    payload: Json = field(default_factory=dict)
    redirect: str = ""


def _result(status: int, payload: Json) -> AdminCatalogMutationResult:
    return AdminCatalogMutationResult(matched=True, success=200 <= status < 300, status=status, payload=payload)


def safe_admin_skill_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or not _SAFE_NAME_RE.fullmatch(text):
        return ""
    return text


def sanitized_admin_skill_name(value: Any) -> str:
    return safe_admin_skill_name(re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-"))


def admin_skills_root() -> pathlib.Path:
    configured = str(os.environ.get("GATEWAY_ADMIN_SKILLS_ROOT") or "").strip()
    if configured:
        return pathlib.Path(configured).expanduser().resolve(strict=False)
    return pathlib.Path.cwd().resolve(strict=False) / "skills"


def admin_skill_dir(name: Any, *, root: pathlib.Path | None = None) -> pathlib.Path | None:
    safe_name = safe_admin_skill_name(name)
    if not safe_name:
        return None
    skills_root = pathlib.Path(root) if root is not None else admin_skills_root()
    return skills_root / safe_name


def admin_skill_file(skill_dir: pathlib.Path) -> pathlib.Path | None:
    raw_dir = pathlib.Path(skill_dir)
    raw_file = raw_dir / "SKILL.md"
    if raw_dir.is_symlink() or raw_file.is_symlink():
        return None
    try:
        resolved_dir = raw_dir.resolve(strict=False)
        resolved_file = raw_file.resolve(strict=False)
        resolved_file.relative_to(resolved_dir)
    except (OSError, ValueError):
        return None
    return raw_file


def _require_owned_directory(directory_fd: int, label: str) -> os.stat_result:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory")
    current_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
    if metadata.st_uid != current_uid:
        raise ValueError(f"{label} must be owned by the Gateway user")
    os.fchmod(directory_fd, 0o700)
    return metadata


def _open_catalog_root(root: pathlib.Path) -> int:
    if root.is_symlink():
        raise ValueError("skills root must not be a symlink")
    try:
        root_fd = os.open(str(root), _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise ValueError("skills root must be a non-symlink directory") from exc
    try:
        _require_owned_directory(root_fd, "skills root")
    except Exception:
        os.close(root_fd)
        raise
    return root_fd


def _ensure_catalog_root(root: pathlib.Path) -> int:
    if root.is_symlink():
        raise ValueError("skills root must not be a symlink")
    if root.exists():
        if not root.is_dir():
            raise ValueError("skills root must be a directory")
        return _open_catalog_root(root)
    root.mkdir(parents=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError as exc:
        raise ValueError("unable to secure skills root permissions") from exc
    fsync_directory(root.parent)
    return _open_catalog_root(root)


def _open_skill_directory(root_fd: int, name: str) -> tuple[int, bool]:
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=root_fd)
        created = True
        os.fsync(root_fd)
    except FileExistsError:
        pass
    try:
        skill_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd)
    except OSError as exc:
        raise ValueError("skill target must be a non-symlink directory") from exc
    try:
        opened = _require_owned_directory(skill_fd, "skill directory")
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("skill directory changed during mutation")
    except Exception:
        os.close(skill_fd)
        if created:
            try:
                os.rmdir(name, dir_fd=root_fd)
                os.fsync(root_fd)
            except OSError:
                pass
        raise
    return skill_fd, created


def _write_all(file_fd: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(file_fd, view[offset:])
        if written <= 0:
            raise OSError("skill file write made no progress")
        offset += written


def _atomic_write_skill_file(skill_fd: int, content: str) -> None:
    try:
        existing = os.stat("SKILL.md", dir_fd=skill_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise ValueError("SKILL.md must be a regular non-symlink file")

    temporary_name = f".SKILL.md.gateway-{uuid.uuid4().hex}.tmp"
    temporary_fd = -1
    try:
        temporary_fd = os.open(temporary_name, _FILE_CREATE_FLAGS, 0o600, dir_fd=skill_fd)
        os.fchmod(temporary_fd, 0o600)
        _write_all(temporary_fd, content.encode("utf-8"))
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        os.replace(temporary_name, "SKILL.md", src_dir_fd=skill_fd, dst_dir_fd=skill_fd)
        os.fsync(skill_fd)
        installed = os.stat("SKILL.md", dir_fd=skill_fd, follow_symlinks=False)
        if not stat.S_ISREG(installed.st_mode):
            raise ValueError("installed SKILL.md must be a regular file")
        os.fsync(skill_fd)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=skill_fd)
        except FileNotFoundError:
            pass


def _rollback_created_skill(root_fd: int, skill_fd: int, name: str) -> None:
    try:
        os.unlink("SKILL.md", dir_fd=skill_fd)
    except FileNotFoundError:
        pass
    try:
        os.rmdir(name, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError:
        pass


def _write_skill(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    target = admin_skill_dir(name, root=root)
    if target is None:
        raise ValueError("invalid skill name")
    with path_write_lock(root):
        root_fd = _ensure_catalog_root(root)
        skill_fd = -1
        created = False
        try:
            skill_fd, created = _open_skill_directory(root_fd, name)
            opened = os.fstat(skill_fd)
            _atomic_write_skill_file(skill_fd, content)
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("skill directory changed during mutation")
        except Exception:
            if created and skill_fd >= 0:
                _rollback_created_skill(root_fd, skill_fd, name)
            raise
        finally:
            if skill_fd >= 0:
                os.close(skill_fd)
            os.close(root_fd)
        return target / "SKILL.md"


def _remove_directory_contents(directory_fd: int) -> None:
    for entry in os.listdir(directory_fd):
        metadata = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(entry, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise ValueError("skill directory changed during deletion")
                _remove_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(entry, dir_fd=directory_fd)
        else:
            os.unlink(entry, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _delete_skill(root: pathlib.Path, name: str) -> bool:
    target = admin_skill_dir(name, root=root)
    if target is None:
        raise ValueError("invalid skill name")
    with path_write_lock(root):
        root_fd = _ensure_catalog_root(root)
        try:
            try:
                metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("skill target must be a non-symlink directory")
            target_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd)
            try:
                opened = _require_owned_directory(target_fd, "skill directory")
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise ValueError("skill directory changed during deletion")
            finally:
                os.close(target_fd)

            tombstone = f".{name}.delete-{uuid.uuid4().hex}"
            os.replace(name, tombstone, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
            tombstone_fd = -1
            try:
                tombstone_fd = os.open(tombstone, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd)
                moved = os.fstat(tombstone_fd)
                if (moved.st_dev, moved.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise ValueError("skill target changed during deletion")
                _remove_directory_contents(tombstone_fd)
                os.close(tombstone_fd)
                tombstone_fd = -1
                os.rmdir(tombstone, dir_fd=root_fd)
                os.fsync(root_fd)
            except Exception:
                if tombstone_fd >= 0:
                    os.close(tombstone_fd)
                try:
                    os.stat(tombstone, dir_fd=root_fd, follow_symlinks=False)
                    os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        os.replace(tombstone, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                        os.fsync(root_fd)
                    except OSError:
                        pass
                raise
            return True
        finally:
            os.close(root_fd)


def _install_mcp_marketplace(
    mcp_id: str,
    server: Json,
    *,
    reload_mcp: Callable[[], None],
) -> AdminCatalogMutationResult:
    from .gateway_config import load_config_with_revision, save_config

    package = str(server.get("package") or "").strip()
    if not package or not _SAFE_NPM_PACKAGE_RE.fullmatch(package):
        return _result(400, {"error": "invalid MCP marketplace package"})
    config, revision = load_config_with_revision()
    candidate = copy.deepcopy(config)
    raw_mcp = candidate.get("mcp")
    if raw_mcp is None:
        mcp: Json = {}
        candidate["mcp"] = mcp
    elif isinstance(raw_mcp, dict):
        mcp = raw_mcp
    else:
        return _result(400, {"error": "invalid MCP server configuration"})
    raw_servers = mcp.get("servers")
    if raw_servers is None:
        servers: list[Json] = []
        mcp["servers"] = servers
    elif isinstance(raw_servers, list) and all(isinstance(item, dict) for item in raw_servers):
        servers = raw_servers
    else:
        return _result(400, {"error": "invalid MCP server configuration"})
    if any(str(item.get("name") or "") == mcp_id for item in servers):
        return _result(200, {"ok": True, "message": "already installed"})
    servers.append(
        {
            "name": mcp_id,
            "command": "npx",
            "args": ["-y", package],
            "enabled": True,
        }
    )
    save_config(candidate, expected_revision=revision)
    try:
        reload_mcp()
    except Exception as exc:
        return _result(
            200,
            {
                "ok": True,
                "name": mcp_id,
                "persisted": True,
                "runtime_reloaded": False,
                "warning": f"MCP configuration saved but runtime reload failed: {exc}",
            },
        )
    return _result(200, {"ok": True, "name": mcp_id, "persisted": True, "runtime_reloaded": True})


def _close_mcp_sessions() -> None:
    from .gateway_mcp import _mcp_close_sessions

    _mcp_close_sessions()


def apply_admin_catalog_mutation(
    path: str,
    payload: Json,
    *,
    skills_root: pathlib.Path | None = None,
    get_skill: Callable[[str], Json | None] | None = None,
    get_mcp_server: Callable[[str], Json | None] | None = None,
    reload_mcp: Callable[[], None] | None = None,
) -> AdminCatalogMutationResult:
    if path not in _PATHS:
        return AdminCatalogMutationResult(matched=False)
    root = pathlib.Path(skills_root) if skills_root is not None else admin_skills_root()

    if path == "/admin/skill-create":
        name = sanitized_admin_skill_name(payload.get("skill_name"))
        content = str(payload.get("skill_content") or "").strip()
        if not name or not content:
            return _result(400, {"error": "skill_name and skill_content required"})
        try:
            _write_skill(root, name, content)
        except ValueError as exc:
            return _result(400, {"error": str(exc)})
        return AdminCatalogMutationResult(
            matched=True,
            success=True,
            status=200,
            payload={"ok": True, "name": name},
            redirect="/ui#skills",
        )

    if path == "/admin/skill-install.json":
        skill_id = safe_admin_skill_name(payload.get("id"))
        if not skill_id:
            return _result(400, {"error": "id required or invalid"})
        if get_skill is None:
            from .marketplace import get_skill_by_id

            get_skill = get_skill_by_id
        skill = get_skill(skill_id)
        if not skill:
            return _result(404, {"error": f"skill not found: {skill_id}"})
        content = "# " + str(skill.get("name") or skill_id) + "\n\n" + str(skill.get("description") or "") + "\n"
        try:
            _write_skill(root, skill_id, content)
        except ValueError as exc:
            return _result(400, {"error": str(exc)})
        return _result(200, {"ok": True, "name": skill_id})

    if path == "/admin/skill-delete.json":
        name = safe_admin_skill_name(payload.get("name"))
        if not name:
            return _result(400, {"error": "name required or invalid"})
        try:
            deleted = _delete_skill(root, name)
        except ValueError as exc:
            return _result(400, {"error": str(exc)})
        if not deleted:
            return _result(404, {"error": "skill not found"})
        return _result(200, {"ok": True})

    mcp_id = safe_admin_skill_name(payload.get("id"))
    if not mcp_id:
        return _result(400, {"error": "id required or invalid"})
    if get_mcp_server is None:
        from .marketplace import get_mcp_server_by_id

        get_mcp_server = get_mcp_server_by_id
    server = get_mcp_server(mcp_id)
    if not server:
        return _result(404, {"error": f"MCP server not found: {mcp_id}"})
    return _install_mcp_marketplace(
        mcp_id,
        server,
        reload_mcp=reload_mcp or _close_mcp_sessions,
    )


_safe_admin_skill_name = safe_admin_skill_name
_admin_skills_root = admin_skills_root
_admin_skill_dir = admin_skill_dir
_admin_skill_file = admin_skill_file


__all__ = [
    "AdminCatalogMutationResult",
    "_admin_skill_dir",
    "_admin_skill_file",
    "_admin_skills_root",
    "_safe_admin_skill_name",
    "admin_skill_dir",
    "admin_skill_file",
    "admin_skills_root",
    "apply_admin_catalog_mutation",
    "safe_admin_skill_name",
    "sanitized_admin_skill_name",
]
