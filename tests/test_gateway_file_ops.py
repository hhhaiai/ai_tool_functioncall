import concurrent.futures
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

from src.gateway_file_ops import atomic_update_text, atomic_write_text


def test_atomic_write_preserves_existing_mode_and_cleans_temp_files():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        target = root / "script.sh"
        target.write_text("old\n", encoding="utf-8")
        target.chmod(0o750)

        atomic_write_text(target, "new\n")

        assert target.read_text(encoding="utf-8") == "new\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o750
        assert list(root.glob(".script.sh.gateway-*.tmp")) == []


def test_atomic_write_new_file_defaults_to_private_mode():
    with tempfile.TemporaryDirectory() as td:
        target = pathlib.Path(td) / "new.txt"
        atomic_write_text(target, "private")
        assert target.read_text(encoding="utf-8") == "private"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_create_bytes_has_exactly_one_winner():
    from src.gateway_file_ops import atomic_create_bytes

    with tempfile.TemporaryDirectory() as td:
        target = pathlib.Path(td) / "create-once.bin"

        def create(index: int) -> tuple[int, bool]:
            return index, atomic_create_bytes(target, f"value-{index}".encode("utf-8"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(create, range(50)))

        winners = [index for index, won in results if won]
        assert len(winners) == 1
        assert target.read_bytes() == f"value-{winners[0]}".encode("utf-8")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_replace_failure_preserves_original_and_removes_temp():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        target = root / "important.txt"
        target.write_text("original", encoding="utf-8")

        with patch("src.gateway_file_ops.os.replace", side_effect=OSError("injected replace failure")):
            with pytest.raises(OSError, match="injected replace failure"):
                atomic_write_text(target, "replacement")

        assert target.read_text(encoding="utf-8") == "original"
        assert list(root.glob(".important.txt.gateway-*.tmp")) == []


def test_atomic_write_rejects_symlink_escape_from_allowed_root():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        allowed = root / "allowed"
        allowed.mkdir()
        outside = root / "outside.txt"
        outside.write_text("untouched", encoding="utf-8")
        link = allowed / "SKILL.md"
        link.symlink_to(outside)

        with pytest.raises(ValueError, match="escapes allowed root"):
            atomic_write_text(link, "malicious replacement", allowed_root=allowed)

        assert outside.read_text(encoding="utf-8") == "untouched"


def test_admin_skill_file_rejects_preexisting_symlink_escape():
    from src.gateway_http_handler import _admin_skill_file

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        skill_dir = root / "skills" / "safe"
        skill_dir.mkdir(parents=True)
        outside = root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (skill_dir / "SKILL.md").symlink_to(outside)
        assert _admin_skill_file(skill_dir) is None


def test_threaded_atomic_updates_do_not_lose_increments():
    with tempfile.TemporaryDirectory() as td:
        target = pathlib.Path(td) / "counter.txt"
        target.write_text("0", encoding="utf-8")

        def increment(_: int) -> None:
            atomic_update_text(
                target,
                lambda current: (str(int(current) + 1), None),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(increment, range(120)))

        assert target.read_text(encoding="utf-8") == "120"


def test_cross_process_atomic_updates_share_runtime_lock():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        target = root / "counter.txt"
        target.write_text("0", encoding="utf-8")
        env = {
            **os.environ,
            "GATEWAY_RUNTIME_DIR": str(root / "runtime"),
        }
        code = (
            "import pathlib,sys; "
            "from src.gateway_file_ops import atomic_update_text; "
            "p=pathlib.Path(sys.argv[1]); "
            "[(atomic_update_text(p, lambda value: (str(int(value)+1), None))) for _ in range(50)]"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(target)],
                cwd=pathlib.Path(__file__).resolve().parents[1],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, f"stdout={stdout}\nstderr={stderr}"

        assert target.read_text(encoding="utf-8") == "100"


def test_json_fallback_stats_do_not_lose_concurrent_updates():
    from src import gateway_logging as gateway_log

    with tempfile.TemporaryDirectory() as td:
        stats_path = pathlib.Path(td) / "stats.json"
        with patch.object(gateway_log, "STATS_PATH", stats_path), patch.object(
            gateway_log,
            "_logging_backend",
            return_value="file",
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                list(executor.map(lambda _: gateway_log._record_tool_stat("Read", True), range(100)))
                list(executor.map(lambda _: gateway_log._record_request_stat("/v1/messages", 200), range(100)))

        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        assert stats["tools"]["Read"]["calls"] == 100
        assert stats["tools"]["Read"]["success"] == 100
        assert stats["requests"]["total"] == 100
        assert stats["requests"]["/v1/messages"] == 100
        assert stats["requests"]["2xx"] == 100


def test_jsonl_fallback_appends_are_complete_under_concurrency():
    from src import gateway_logging as gateway_log

    with tempfile.TemporaryDirectory() as td:
        log_path = pathlib.Path(td) / "events.jsonl"
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            list(
                executor.map(
                    lambda index: gateway_log._write_jsonl_file(log_path, {"index": index}),
                    range(100),
                )
            )

        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 100
        assert {row["index"] for row in rows} == set(range(100))
