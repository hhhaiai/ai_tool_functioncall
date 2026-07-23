import json
import concurrent.futures
import os
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import src.toolcall_gateway as gateway
from src import gateway_builtin_tools, gateway_cache
from src.gateway_persistence import PersistenceConfig, close_persistence, init_persistence
from src.toolcall_gateway import execute_direct_tool_call


class ToolRuntimeStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.old_config = gateway.CONFIG_PATH
        self.old_write = os.environ.get("GATEWAY_ALLOW_WRITE_TOOLS")
        self.old_shell = os.environ.get("GATEWAY_ALLOW_SHELL_TOOLS")
        gateway.CONFIG_PATH = self.root / "config.json"
        os.environ["GATEWAY_ALLOW_WRITE_TOOLS"] = "1"
        os.environ["GATEWAY_ALLOW_SHELL_TOOLS"] = "1"

        config = gateway._default_config()
        config["gateway"]["allow_write_tools"] = True
        config["gateway"]["allow_shell_tools"] = True
        config["gateway"]["execute_user_side_tools_in_gateway"] = True
        config["gateway"]["tool_cache_persist_local_results"] = True
        config["persistence"]["enabled"] = True
        config["persistence"]["db_path"] = str(self.root / "tool-cache.db")
        gateway.save_config(config)
        init_persistence(PersistenceConfig(enabled=True, db_path=str(self.root / "tool-cache.db")))
        gateway_cache.reset_caches()

    def tearDown(self):
        gateway_cache.reset_caches()
        close_persistence()
        gateway.CONFIG_PATH = self.old_config
        if self.old_write is None:
            os.environ.pop("GATEWAY_ALLOW_WRITE_TOOLS", None)
        else:
            os.environ["GATEWAY_ALLOW_WRITE_TOOLS"] = self.old_write
        if self.old_shell is None:
            os.environ.pop("GATEWAY_ALLOW_SHELL_TOOLS", None)
        else:
            os.environ["GATEWAY_ALLOW_SHELL_TOOLS"] = self.old_shell
        self.temp_dir.cleanup()

    def call(self, tool, arguments, call_id, *, workspace=None, client_id="stability-client"):
        return execute_direct_tool_call(
            {
                "workspace_root": str(workspace or self.workspace),
                "session_id": "stability-session",
                "tool": tool,
                "arguments": arguments,
                "call_id": call_id,
            },
            client_id=client_id,
        )

    def test_nonzero_process_exits_are_protocol_errors(self):
        bash = self.call("Bash", {"command": "exit 7"}, "bash-failure")
        code = self.call("code_interpreter", {"code": "raise SystemExit(3)"}, "code-failure")

        for result, exit_code in ((bash, 7), (code, 3)):
            self.assertFalse(result["success"])
            self.assertEqual(result["failure_type"], "execution_failed")
            self.assertTrue(result["anthropic"]["is_error"])
            self.assertIn(str(exit_code), result["content"])

        parsed_code = json.loads(code["content"].removeprefix("execution_failed: "))
        self.assertEqual(parsed_code["exit_code"], 3)

    def test_tool_processes_do_not_inherit_gateway_credentials(self):
        secrets = {
            "UPSTREAM_API_KEY": "upstream-secret-canary",
            "GATEWAY_DOWNSTREAM_KEY": "downstream-secret-canary",
            "OPENAI_API_KEY": "openai-secret-canary",
        }
        probe = (
            "import os; "
            "print('|'.join(str(os.getenv(k)) for k in "
            "['UPSTREAM_API_KEY','GATEWAY_DOWNSTREAM_KEY','OPENAI_API_KEY']))"
        )
        with patch.dict(os.environ, {**secrets, "GATEWAY_TOOL_ENV_ALLOWLIST": ""}, clear=False):
            bash = self.call("Bash", {"command": f"python3 -c \"{probe}\""}, "env-bash")
            code = self.call("code_interpreter", {"code": probe}, "env-code")
            exec_result = self.call(
                "exec_shell_start",
                {"session_id": "env-exec", "command": f"python3 -c \"{probe}\"", "read_timeout": 0.3},
                "env-exec-start",
            )

        for result in (bash, code, exec_result):
            self.assertTrue(result["success"], result)
            self.assertIn("None|None|None", result["content"])
            for secret in secrets.values():
                self.assertNotIn(secret, result["content"])

    def test_read_and_directory_caches_refresh_after_mutations_and_restart(self):
        target = self.workspace / "app.txt"
        target.write_text("old-value\n", encoding="utf-8")

        first_read = self.call("Read", {"file_path": "app.txt"}, "read-before")
        first_glob = self.call("Glob", {"pattern": "*.txt"}, "glob-before")
        self.assertIn("old-value", first_read["content"])
        self.assertNotIn("added.txt", first_glob["content"])

        edit = self.call(
            "Edit",
            {"file_path": "app.txt", "old_string": "old-value", "new_string": "new-value"},
            "edit",
        )
        write = self.call("Write", {"file_path": "added.txt", "content": "created\n"}, "write")
        self.assertTrue(edit["success"])
        self.assertTrue(write["success"])

        second_read = self.call("Read", {"file_path": "app.txt"}, "read-after")
        second_glob = self.call("Glob", {"pattern": "*.txt"}, "glob-after")
        self.assertIn("new-value", second_read["content"])
        self.assertIn("added.txt", second_glob["content"])

        gateway_cache.reset_caches()
        restarted_read = self.call("Read", {"file_path": "app.txt"}, "read-after-restart")
        self.assertIn("new-value", restarted_read["content"])
        self.assertNotIn("old-value", restarted_read["content"])

    def test_failed_shell_with_partial_side_effect_invalidates_scope(self):
        target = self.workspace / "partial.txt"
        target.write_text("before\n", encoding="utf-8")
        cached = self.call("Read", {"file_path": "partial.txt"}, "partial-read-before")
        self.assertIn("before", cached["content"])

        failed = self.call(
            "Bash",
            {"command": "printf 'after\\n' > partial.txt; exit 9"},
            "partial-bash",
        )
        self.assertFalse(failed["success"])

        refreshed = self.call("Read", {"file_path": "partial.txt"}, "partial-read-after")
        self.assertIn("after", refreshed["content"])
        self.assertNotIn("before", refreshed["content"])

    def test_scope_invalidation_does_not_evict_another_workspace(self):
        workspace_a = self.root / "workspace-a"
        workspace_b = self.root / "workspace-b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        (workspace_a / "value.txt").write_text("A-old\n", encoding="utf-8")
        (workspace_b / "value.txt").write_text("B-old\n", encoding="utf-8")

        self.call("Read", {"file_path": "value.txt"}, "read-a", workspace=workspace_a)
        self.call("Read", {"file_path": "value.txt"}, "read-b", workspace=workspace_b)
        cache = gateway_cache.get_tool_result_cache()
        self.assertEqual(cache.stats["entries"], 2)

        edited = self.call(
            "Edit",
            {"file_path": "value.txt", "old_string": "A-old", "new_string": "A-new"},
            "edit-a",
            workspace=workspace_a,
        )
        self.assertTrue(edited["success"])
        self.assertEqual(cache.stats["entries"], 1)

        with sqlite3.connect(self.root / "tool-cache.db") as conn:
            remaining = conn.execute("SELECT workspace_key FROM tool_cache").fetchall()
        self.assertEqual(remaining, [(str(workspace_b.resolve()),)])

    def test_idempotent_http_action_retries_but_post_does_not(self):
        class RetryHandler(BaseHTTPRequestHandler):
            get_calls = 0
            post_calls = 0

            def log_message(self, fmt, *args):
                return

            def do_GET(self):  # noqa: N802
                RetryHandler.get_calls += 1
                status = 503 if RetryHandler.get_calls == 1 else 200
                payload = b"retry" if status == 503 else b"ok"
                self.send_response(status)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):  # noqa: N802
                RetryHandler.post_calls += 1
                payload = b"do-not-repeat"
                self.send_response(503)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = gateway.load_config()
            config["http_actions"] = {
                "enabled": True,
                "actions": [
                    {
                        "name": "retry_get",
                        "method": "GET",
                        "url": f"http://127.0.0.1:{server.server_address[1]}/get",
                        "allow_private_network": True,
                        "max_retries": 1,
                    },
                    {
                        "name": "no_retry_post",
                        "method": "POST",
                        "url": f"http://127.0.0.1:{server.server_address[1]}/post",
                        "allow_private_network": True,
                        "max_retries": 3,
                    },
                ],
            }
            gateway.save_config(config)

            get_result = self.call("retry_get", {}, "retry-get")
            post_result = self.call("no_retry_post", {}, "no-retry-post")
            self.assertTrue(get_result["success"])
            self.assertEqual(RetryHandler.get_calls, 2)
            self.assertFalse(post_result["success"])
            self.assertEqual(post_result["failure_type"], "http_action_failed")
            self.assertEqual(RetryHandler.post_calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_concurrent_canonical_edits_do_not_overwrite_each_other(self):
        tokens = [f"token-{index}" for index in range(24)]
        target = self.workspace / "concurrent.txt"
        target.write_text(" ".join(tokens), encoding="utf-8")

        def edit(index):
            return self.call(
                "Edit",
                {
                    "file_path": "concurrent.txt",
                    "old_string": tokens[index],
                    "new_string": tokens[index].upper(),
                },
                f"concurrent-edit-{index}",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(edit, range(len(tokens))))

        self.assertTrue(all(result["success"] for result in results), results)
        final_text = target.read_text(encoding="utf-8")
        for token in tokens:
            self.assertIn(token.upper(), final_text)

    def test_read_cache_is_bypassed_while_scoped_exec_process_is_active(self):
        target = self.workspace / "background.txt"
        target.write_text("before\n", encoding="utf-8")
        self.call("Read", {"file_path": "background.txt"}, "background-before")

        command = (
            "printf 'phase-one\\n' > background.txt; printf 'phase-one\\n'; "
            "python3 -u -c \"import pathlib,time; "
            "p=pathlib.Path('background.txt'); "
            "time.sleep(0.8); p.write_text('phase-two\\n'); print('phase-two', flush=True); "
            "time.sleep(0.5)\""
        )
        started = self.call(
            "exec_shell_start",
            {"session_id": "background-writer", "command": command, "read_timeout": 0.5},
            "background-start",
        )
        self.assertTrue(started["success"])
        self.assertIn("phase-one", started["content"])

        first = self.call("Read", {"file_path": "background.txt"}, "background-phase-one")
        self.assertIn("phase-one", first["content"])
        time.sleep(0.9)
        second = self.call("Read", {"file_path": "background.txt"}, "background-phase-two")
        self.assertIn("phase-two", second["content"])
        self.assertNotIn("phase-one", second["content"])

        waited = self.call(
            "exec_wait",
            {"session_id": "background-writer", "timeout": 2},
            "background-wait",
        )
        self.assertTrue(waited["success"])

    def test_bash_and_code_output_floods_are_captured_with_fixed_bounds(self):
        with patch.dict(os.environ, {"GATEWAY_TOOL_OUTPUT_MAX_CHARS": "1024"}, clear=False):
            bash = self.call(
                "Bash",
                {
                    "command": (
                        "python3 -c \"import sys; "
                        "sys.stdout.write('A'*2000000); sys.stderr.write('B'*2000000)\""
                    ),
                    "timeout": 10,
                },
                "bash-output-flood",
            )
            code = self.call(
                "code_interpreter",
                {"code": "import sys; print('C'*2000000); print('D'*2000000, file=sys.stderr)", "timeout": 10},
                "code-output-flood",
            )

        self.assertTrue(bash["success"])
        self.assertTrue(code["success"])
        self.assertLess(len(bash["content"]), 2600)
        self.assertLess(len(code["content"]), 2600)
        self.assertGreaterEqual(bash["content"].count("gateway: truncated"), 2)
        self.assertGreaterEqual(code["content"].count("gateway: truncated"), 2)

    def test_bash_timeout_keeps_partial_output_and_kills_descendants(self):
        command = (
            "(sleep 1; printf bad > child-survived) & child=$!; "
            "printf 'CHILD-READY\\n'; printf yes > child-started; wait \"$child\""
        )
        timed_out = self.call("Bash", {"command": command, "timeout": 0.25}, "bash-timeout-tree")
        self.assertFalse(timed_out["success"])
        self.assertEqual(timed_out["failure_type"], "timeout")
        self.assertIn("CHILD-READY", timed_out["content"])
        self.assertTrue((self.workspace / "child-started").exists())
        time.sleep(1.1)
        self.assertFalse((self.workspace / "child-survived").exists())

    def test_successful_shell_does_not_leave_background_descendants(self):
        command = (
            "python3 -c \"import pathlib,time; time.sleep(1); "
            "pathlib.Path('background-survived').write_text('bad')\" & exit 0"
        )
        result = self.call("Bash", {"command": command, "timeout": 5}, "bash-background-cleanup")
        self.assertTrue(result["success"])
        time.sleep(1.1)
        self.assertFalse((self.workspace / "background-survived").exists())

    def test_exec_start_immediate_nonzero_is_error_and_session_is_removed(self):
        failed = self.call(
            "exec_shell_start",
            {"session_id": "immediate-fail", "command": "exit 4", "read_timeout": 0.2},
            "exec-immediate-fail",
        )
        self.assertFalse(failed["success"])
        self.assertEqual(failed["failure_type"], "execution_failed")
        self.assertTrue(failed["anthropic"]["is_error"])
        self.assertIn('"exit_code": 4', failed["content"])

        missing = self.call(
            "exec_wait",
            {"session_id": "immediate-fail", "timeout": 0.1},
            "exec-immediate-missing",
        )
        self.assertFalse(missing["success"])
        self.assertEqual(missing["failure_type"], "not_found")

    def test_exec_wait_and_write_stdin_report_nonzero_terminal_exit(self):
        wait_start = self.call(
            "exec_shell_start",
            {
                "session_id": "wait-fail",
                "command": "python3 -u -c \"import time; print('WAITING', flush=True); time.sleep(.15); raise SystemExit(5)\"",
                "read_timeout": 0.02,
            },
            "exec-wait-start",
        )
        self.assertTrue(wait_start["success"])
        waited = self.call(
            "exec_wait",
            {"session_id": "wait-fail", "timeout": 2},
            "exec-wait-fail",
        )
        self.assertFalse(waited["success"])
        self.assertIn('"exit_code": 5', waited["content"])

        stdin_start = self.call(
            "exec_shell_start",
            {
                "session_id": "stdin-fail",
                "command": "python3 -u -c \"import sys; print(sys.stdin.readline().strip(), flush=True); raise SystemExit(6)\"",
                "read_timeout": 0.02,
            },
            "exec-stdin-start",
        )
        self.assertTrue(stdin_start["success"])
        written = self.call(
            "write_stdin",
            {"session_id": "stdin-fail", "chars": "INPUT-SEEN\n", "read_timeout": 0.3},
            "exec-stdin-fail",
        )
        contents = [written["content"]]
        if written["success"]:
            time.sleep(0.1)
            gateway_builtin_tools._reap_expired_exec_sessions(now=time.time() + 2)
            written = self.call(
                "write_stdin",
                {"session_id": "stdin-fail", "chars": "", "read_timeout": 0.1},
                "exec-stdin-terminal",
            )
            contents.append(written["content"])
        self.assertFalse(written["success"])
        combined = "\n".join(contents)
        self.assertIn("INPUT-SEEN", combined)
        self.assertIn('"exit_code": 6', combined)

    def test_apply_patch_rejects_workspace_escape_and_symlink_targets(self):
        outside = self.root / "outside.txt"
        escaped = self.call(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Add File: ../outside.txt\n"
                    "+escaped\n"
                    "*** End Patch\n"
                )
            },
            "patch-escape",
        )
        self.assertFalse(escaped["success"])
        self.assertEqual(escaped["failure_type"], "permission_denied")
        self.assertFalse(outside.exists())

        outside.write_text("outside-safe\n", encoding="utf-8")
        (self.workspace / "linked.txt").symlink_to(outside)
        symlinked = self.call(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: linked.txt\n"
                    "@@\n"
                    "-outside-safe\n"
                    "+changed\n"
                    "*** End Patch\n"
                )
            },
            "patch-symlink",
        )
        self.assertFalse(symlinked["success"])
        self.assertEqual(symlinked["failure_type"], "permission_denied")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside-safe\n")

    def test_apply_patch_success_and_partial_failure_rollback(self):
        successful = self.call(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Add File: safe.txt\n"
                    "+safe-content\n"
                    "*** End Patch\n"
                )
            },
            "patch-safe",
        )
        self.assertTrue(successful["success"], successful)
        self.assertEqual((self.workspace / "safe.txt").read_text(encoding="utf-8"), "safe-content\n")

        move_source = self.workspace / "move-source.txt"
        move_source.write_text("move-old\n", encoding="utf-8")
        moved = self.call(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: move-source.txt\n"
                    "*** Move to: move-destination.txt\n"
                    "@@\n"
                    "-move-old\n"
                    "+move-new\n"
                    "*** End Patch\n"
                )
            },
            "patch-move",
        )
        self.assertTrue(moved["success"], moved)
        self.assertFalse(move_source.exists())
        self.assertEqual(
            (self.workspace / "move-destination.txt").read_text(encoding="utf-8"),
            "move-new\n",
        )

        victim = self.workspace / "victim.txt"
        victim.write_text("original\n", encoding="utf-8")
        fake = self.root / "partial-patch.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "Path('victim.txt').write_text('partial\\n')\n"
            "Path('new.txt').write_text('partial\\n')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: victim.txt\n"
            "@@\n"
            "-original\n"
            "+updated\n"
            "*** Add File: new.txt\n"
            "+new\n"
            "*** End Patch\n"
        )
        with patch.dict(os.environ, {"GATEWAY_APPLY_PATCH_BIN": str(fake)}, clear=False):
            failed = self.call("apply_patch", {"patch": patch_text}, "patch-partial-failure")
        self.assertFalse(failed["success"])
        self.assertEqual(victim.read_text(encoding="utf-8"), "original\n")
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_apply_patch_timeout_rolls_back_partial_changes(self):
        victim = self.workspace / "timeout-victim.txt"
        victim.write_text("original\n", encoding="utf-8")
        fake = self.root / "timeout-patch.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import time\n"
            "Path('timeout-victim.txt').write_text('partial\\n')\n"
            "time.sleep(5)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: timeout-victim.txt\n"
            "@@\n"
            "-original\n"
            "+updated\n"
            "*** End Patch\n"
        )
        with patch.dict(
            os.environ,
            {"GATEWAY_APPLY_PATCH_BIN": str(fake), "GATEWAY_APPLY_PATCH_TIMEOUT": "0.2"},
            clear=False,
        ):
            failed = self.call("apply_patch", {"patch": patch_text}, "patch-timeout")
        self.assertFalse(failed["success"])
        self.assertEqual(failed["failure_type"], "timeout")
        self.assertEqual(victim.read_text(encoding="utf-8"), "original\n")

    def test_apply_patch_reports_rollback_failure(self):
        first = self.workspace / "rollback-first.txt"
        second = self.workspace / "rollback-second.txt"
        first.write_text("first-original\n", encoding="utf-8")
        second.write_text("second-original\n", encoding="utf-8")
        fake = self.root / "rollback-fail-patch.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "Path('rollback-first.txt').write_text('first-updated\\n')\n"
            "Path('rollback-second.txt').write_text('second-updated\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: rollback-first.txt\n"
            "@@\n"
            "-first-original\n"
            "+first-updated\n"
            "*** Update File: rollback-second.txt\n"
            "@@\n"
            "-second-original\n"
            "+second-updated\n"
            "*** End Patch\n"
        )
        real_replace = gateway_builtin_tools.replace_bytes_locked
        replace_calls = 0

        def injected_replace(path, data, *, mode=0o600):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 1:
                return real_replace(path, data, mode=mode)
            if replace_calls == 2:
                raise OSError("injected commit failure")
            raise OSError("injected rollback failure")

        with patch.dict(os.environ, {"GATEWAY_APPLY_PATCH_BIN": str(fake)}, clear=False), patch(
            "src.gateway_builtin_tools.replace_bytes_locked",
            side_effect=injected_replace,
        ):
            failed = self.call("apply_patch", {"patch": patch_text}, "patch-rollback-failure")
        self.assertFalse(failed["success"])
        self.assertIn("rollback_failed", failed["content"])

    def test_apply_patch_commit_failure_rolls_back_committed_targets(self):
        first = self.workspace / "commit-first.txt"
        second = self.workspace / "commit-second.txt"
        first.write_text("first-original\n", encoding="utf-8")
        second.write_text("second-original\n", encoding="utf-8")
        fake = self.root / "commit-fail-patch.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "Path('commit-first.txt').write_text('first-updated\\n')\n"
            "Path('commit-second.txt').write_text('second-updated\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: commit-first.txt\n"
            "@@\n"
            "-first-original\n"
            "+first-updated\n"
            "*** Update File: commit-second.txt\n"
            "@@\n"
            "-second-original\n"
            "+second-updated\n"
            "*** End Patch\n"
        )
        real_replace = gateway_builtin_tools.replace_bytes_locked
        replace_calls = 0

        def fail_second_commit(path, data, *, mode=0o600):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("injected second-target commit failure")
            return real_replace(path, data, mode=mode)

        with patch.dict(os.environ, {"GATEWAY_APPLY_PATCH_BIN": str(fake)}, clear=False), patch(
            "src.gateway_builtin_tools.replace_bytes_locked",
            side_effect=fail_second_commit,
        ):
            failed = self.call("apply_patch", {"patch": patch_text}, "patch-commit-rollback")

        self.assertFalse(failed["success"])
        self.assertIn("patch workspace commit failed", failed["content"])
        self.assertNotIn("rollback_failed", failed["content"])
        self.assertEqual(first.read_text(encoding="utf-8"), "first-original\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "second-original\n")

    def test_apply_patch_rejects_undeclared_overlay_write(self):
        victim = self.workspace / "declared.txt"
        victim.write_text("original\n", encoding="utf-8")
        fake = self.root / "undeclared-patch.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "Path('declared.txt').write_text('updated\\n')\n"
            "Path('sneaky.txt').write_text('undeclared\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: declared.txt\n"
            "@@\n"
            "-original\n"
            "+updated\n"
            "*** End Patch\n"
        )
        with patch.dict(os.environ, {"GATEWAY_APPLY_PATCH_BIN": str(fake)}, clear=False):
            failed = self.call("apply_patch", {"patch": patch_text}, "patch-undeclared-write")

        self.assertFalse(failed["success"])
        self.assertEqual(failed["failure_type"], "permission_denied")
        self.assertIn("undeclared file", failed["content"])
        self.assertEqual(victim.read_text(encoding="utf-8"), "original\n")
        self.assertFalse((self.workspace / "sneaky.txt").exists())

    def test_apply_patch_detects_external_version_conflict(self):
        victim = self.workspace / "conflict.txt"
        victim.write_text("original\n", encoding="utf-8")
        marker = self.root / "overlay-started"
        fake = self.root / "conflict-patch.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import time\n"
            f"Path({str(marker)!r}).write_text('ready')\n"
            "time.sleep(0.25)\n"
            "Path('conflict.txt').write_text('overlay-update\\n')\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: conflict.txt\n"
            "@@\n"
            "-original\n"
            "+overlay-update\n"
            "*** End Patch\n"
        )

        def external_writer():
            deadline = time.time() + 2
            while not marker.exists() and time.time() < deadline:
                time.sleep(0.01)
            victim.write_text("external-update\n", encoding="utf-8")

        writer = threading.Thread(target=external_writer)
        writer.start()
        with patch.dict(os.environ, {"GATEWAY_APPLY_PATCH_BIN": str(fake)}, clear=False):
            failed = self.call("apply_patch", {"patch": patch_text}, "patch-version-conflict")
        writer.join(timeout=2)

        self.assertFalse(failed["success"])
        self.assertEqual(failed["failure_type"], "conflict")
        self.assertEqual(victim.read_text(encoding="utf-8"), "external-update\n")

    def test_concurrent_create_move_delete_have_deterministic_results(self):
        def create(_):
            return self.call(
                "CreateDirectory",
                {"path": "shared-dir", "parents": True, "exist_ok": False},
                f"create-{_}",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            created = list(executor.map(create, range(2)))
        self.assertEqual(sum(result["success"] for result in created), 1)
        self.assertEqual(
            [result["failure_type"] for result in created if not result["success"]],
            ["invalid_input"],
        )

        source = self.workspace / "move-source.txt"
        source.write_text("move", encoding="utf-8")

        def move(index):
            return self.call(
                "MovePath",
                {"source": "move-source.txt", "destination": f"moved-{index}.txt"},
                f"move-{index}",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            moved = list(executor.map(move, range(2)))
        self.assertEqual(sum(result["success"] for result in moved), 1)
        self.assertEqual(
            [result["failure_type"] for result in moved if not result["success"]],
            ["not_found"],
        )

        delete_target = self.workspace / "delete-me.txt"
        delete_target.write_text("delete", encoding="utf-8")

        def delete(index):
            return self.call("DeletePath", {"path": "delete-me.txt"}, f"delete-{index}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            deleted = list(executor.map(delete, range(2)))
        self.assertEqual(sum(result["success"] for result in deleted), 1)
        self.assertEqual(
            [result["failure_type"] for result in deleted if not result["success"]],
            ["not_found"],
        )


if __name__ == "__main__":
    unittest.main()
