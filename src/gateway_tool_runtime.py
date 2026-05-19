def _normalize_tool_call(call: ToolCall) -> ToolCall:
    name = _normalize_tool_name(call.name)
    arguments = _normalize_tool_args(name, call.arguments)
    if name == "Git" and not arguments.get("action"):
        compact = re.sub(r"[^a-z0-9]+", "_", call.name.lower()).strip("_")
        for action in ("status", "diff", "log", "show", "branch"):
            if action in compact:
                arguments["action"] = action
                break
    return ToolCall(
        call_id=call.call_id,
        name=name,
        arguments=arguments,
        raw=call.raw,
    )


def _direct_tool_call_from_body(body: Json) -> ToolCall:
    raw: Json = body
    call_id = str(body.get("id") or body.get("call_id") or body.get("tool_call_id") or f"call_{uuid.uuid4().hex}")
    name: Any = body.get("name") or body.get("tool") or body.get("tool_name") or body.get("function_name") or body.get("recipient_name")
    if isinstance(name, str) and "." in name:
        name = name.rsplit(".", 1)[-1]
    raw_args: Any = body.get("arguments")
    if raw_args is None:
        raw_args = body.get("args")
    if raw_args is None:
        raw_args = body.get("input")
    if raw_args is None:
        raw_args = body.get("parameters")

    function = body.get("function")
    if isinstance(function, dict):
        name = name or function.get("name")
        raw_args = function.get("arguments") if raw_args is None else raw_args
        raw = function

    tool_call = body.get("tool_call")
    if isinstance(tool_call, dict):
        return _direct_tool_call_from_body(tool_call)

    if body.get("type") == "function" and isinstance(body.get("function"), dict):
        function = body["function"]
        name = function.get("name")
        raw_args = function.get("arguments")
        raw = body

    if body.get("type") == "tool_use":
        name = name or body.get("name")
        raw_args = body.get("input") if raw_args is None else raw_args
        raw = body

    if not name:
        raise ToolExecutionError("missing tool/function name", failure_type="invalid_input")
    return ToolCall(
        call_id=call_id,
        name=str(name),
        arguments=_parse_json_arguments(raw_args, allow_text=True),
        raw=raw,
    )


def _direct_tool_calls_from_body(body: Json) -> list[ToolCall]:
    if isinstance(body.get("tool_uses"), list):
        return [
            ToolCall(
                call_id=str(body.get("call_id") or body.get("id") or f"call_{uuid.uuid4().hex}"),
                name="multi_tool_use.parallel",
                arguments={"tool_uses": body.get("tool_uses"), "max_workers": body.get("max_workers")},
                raw=body,
            )
        ]
    raw_calls = body.get("tool_calls") or body.get("calls") or body.get("function_calls")
    if isinstance(raw_calls, list):
        return [_direct_tool_call_from_body(call) for call in raw_calls if isinstance(call, dict)]
    return [_direct_tool_call_from_body(body)]


def _response_tool_call_from_item(item: Json) -> ToolCall | None:
    item_type = item.get("type")
    if item_type not in {"function_call", "tool_call", "custom_tool_call"}:
        return None
    name = item.get("name")
    if not name:
        return None
    raw_args = item.get("arguments")
    allow_text = item_type == "custom_tool_call"
    if raw_args is None and item_type == "custom_tool_call":
        raw_args = item.get("input")
    if raw_args is None:
        raw_args = item.get("input") if isinstance(item.get("input"), dict) else item.get("action")
    return ToolCall(
        call_id=str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"),
        name=str(name),
        arguments=_parse_json_arguments(raw_args, allow_text=allow_text),
        raw=item,
    )


def _strip_xmlish_closing_tags(value: str) -> str:
    return re.sub(r"</(?:parameter|function|tool|invoke)>", "", value, flags=re.I).strip()



def _parse_parameter_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    parameter_re = re.compile(r"<parameter=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<parameter=[A-Za-z0-9_.:-]+>|<function=[A-Za-z0-9_.:-]+>|\Z)", re.S)
    for param in parameter_re.finditer(text or ""):
        key = param.group(1).strip()
        value = _strip_xmlish_closing_tags(param.group(2))
        if key:
            blocks.append((key, value))
    return blocks


def _inline_text_before_parameter_blocks(text: str) -> str:
    return re.sub(r"<parameter=[A-Za-z0-9_.:-]+>.*", "", text or "", flags=re.S).strip()


def _repair_shell_command_spacing(command: str) -> str:
    """Repair common spacing loss from weak text-tool markup."""
    cmd = str(command or "").strip()
    if not cmd:
        return cmd
    cmd = re.sub(r"^(find)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"^(grep)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"^(ls|cat|head|tail|wc|python3?|bash|sh)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"\b(ls\s+-[A-Za-z]+)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"\s-type\s*([fdl])(?=\s|-|$)", r" -type \1", cmd)
    cmd = re.sub(r"(-type\s+[fdl])-name", r"\1 -name", cmd)
    cmd = re.sub(r"\s-name(')", r" -name \1", cmd)
    cmd = re.sub(r'\s-name(")', r' -name \1', cmd)
    cmd = re.sub(r"(?<!\s)-name(')", r" -name \1", cmd)
    cmd = re.sub(r'(?<!\s)-name(")', r' -name \1', cmd)
    cmd = re.sub(r'\s-name([^\s\'"]+)', r" -name \1", cmd)
    cmd = re.sub(r"\b(head|tail)-([0-9]+)\b", r"\1 -\2", cmd)
    cmd = re.sub(r"\b(wc\s+-[A-Za-z]+)\{\}", r"\1 {}", cmd)
    cmd = re.sub(r"\s-l\{\}", r" -l {}", cmd)
    cmd = re.sub(r"([^\s])\{\}(?=\s|$)", r"\1 {}", cmd)
    cmd = re.sub(r"\s+", " ", cmd).strip()
    return cmd

def _parse_text_tool_calls(text: str) -> list[ToolCall]:
    """Parse common text-only tool-call fallbacks emitted by weak native-tool providers."""

    if not text or ("<function=" not in text and "<parameter=" not in text):
        return []
    calls: list[ToolCall] = []
    function_re = re.compile(r"<function=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<function=[A-Za-z0-9_.:-]+>|\Z)", re.S)

    def append_call(name: str, args: Json, raw_text: str) -> None:
        if not name:
            return
        calls.append(
            ToolCall(
                call_id=f"textcall_{uuid.uuid4().hex}",
                name=name,
                arguments=args,
                raw={"gateway_text_tool_call_fallback": True, "text": raw_text[:2000]},
            )
        )

    matched_function = False
    for match in function_re.finditer(text):
        matched_function = True
        name = match.group(1).strip()
        body = match.group(2).strip()
        if body.startswith("{"):
            try:
                parsed = json.loads(_strip_xmlish_closing_tags(body))
                if isinstance(parsed, dict):
                    append_call(name, parsed, match.group(0))
                    continue
            except Exception:
                pass
        blocks = _parse_parameter_blocks(body)
        if name in {"Bash", "bash", "exec_command", "shell", "shell_command"}:
            inline_command = _inline_text_before_parameter_blocks(body)
            if inline_command:
                append_call(name, {"command": _repair_shell_command_spacing(inline_command)}, match.group(0))
            current: Json | None = None
            for key, value in blocks:
                if key in {"command", "cmd", "shell"}:
                    if current and current.get("command"):
                        append_call(name, current, match.group(0))
                    current = {"command": _repair_shell_command_spacing(value)}
                elif current is not None:
                    current[key] = value
            if current and current.get("command"):
                append_call(name, current, match.group(0))
            continue
        args: Json = {}
        for key, value in blocks:
            args[key] = value
        if not args:
            inline_value = _inline_text_before_parameter_blocks(body)
            normalized_name = _normalize_tool_name(name)
            if inline_value and normalized_name in {"Read", "FileInfo", "LS", "Tree", "Glob", "PythonSymbols", "JsonQuery"}:
                if normalized_name == "Glob":
                    args["pattern"] = inline_value
                else:
                    args["path"] = inline_value
        append_call(name, args, match.group(0))

    if not matched_function:
        current: Json | None = None
        for key, value in _parse_parameter_blocks(text):
            if key in {"command", "cmd", "shell"}:
                if current and current.get("command"):
                    append_call("Bash", current, text)
                current = {"command": _repair_shell_command_spacing(value)}
            elif current is not None:
                current[key] = value
        if current and current.get("command"):
            append_call("Bash", current, text)
    return calls


def _extract_tool_calls(path: str, response: Json) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                calls.append(
                    ToolCall(
                        call_id=str(call.get("id") or f"call_{uuid.uuid4().hex}"),
                        name=str(fn["name"]),
                        arguments=_parse_json_arguments(fn.get("arguments")),
                        raw=call,
                    )
                )
        return calls

    if path == "/v1/responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            call = _response_tool_call_from_item(item)
            if call:
                calls.append(call)
            for block in item.get("content") or []:
                if isinstance(block, dict):
                    call = _response_tool_call_from_item(block)
                    if call:
                        calls.append(call)
        return calls

    if path == "/v1/messages":
        for block in response.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name"):
                calls.append(
                    ToolCall(
                        call_id=str(block.get("id") or f"toolu_{uuid.uuid4().hex}"),
                        name=str(block["name"]),
                        arguments=_parse_json_arguments(block.get("input") or {}),
                        raw=block,
                    )
                )
        return calls

    return calls


def _text_tool_call_fallback_enabled() -> bool:
    return bool(_gateway_config().get("text_tool_call_fallback_enabled", True))


def _extract_text_tool_calls(path: str, response: Json) -> list[ToolCall]:
    if not _text_tool_call_fallback_enabled():
        return []
    return _parse_text_tool_calls(_response_text(path, response))


def _assistant_message_from_chat_response(response: Json) -> Json:
    choices = response.get("choices") or []
    if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict):
        return dict(choices[0]["message"])
    return {"role": "assistant", "content": None}


def _append_tool_results(path: str, body: Json, response: Json, results: list[ToolResult]) -> Json:
    updated = dict(body)
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        messages.append(_assistant_message_from_chat_response(response))
        for result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )
        updated["messages"] = messages
        return updated

    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        custom_call_ids: set[str] = set()
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                input_items.append(item)
                if item.get("type") == "custom_tool_call" and item.get("call_id"):
                    custom_call_ids.add(str(item["call_id"]))
            if isinstance(item, dict):
                for block in item.get("content") or []:
                    if isinstance(block, dict) and block.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                        input_items.append(block)
                        if block.get("type") == "custom_tool_call" and block.get("call_id"):
                            custom_call_ids.add(str(block["call_id"]))
        for result in results:
            output_type = "custom_tool_call_output" if result.call_id in custom_call_ids else "function_call_output"
            output_item = {
                "type": output_type,
                "call_id": result.call_id,
                "output": result.content,
            }
            if output_type == "custom_tool_call_output":
                output_item["name"] = result.name
            input_items.append(output_item)
        updated["input"] = input_items
        return updated

    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        content = response.get("content") or []
        messages.append({"role": "assistant", "content": content})
        result_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": result.call_id,
                "content": result.content,
                **({"is_error": True} if not result.success else {}),
            }
            for result in results
        ]
        messages.append({"role": "user", "content": result_blocks})
        updated["messages"] = messages
        return updated

    return updated


def _append_text_tool_results(path: str, body: Json, response: Json, calls: list[ToolCall], results: list[ToolResult]) -> Json:
    updated = dict(body)
    tool_report = {
        "gateway_local_tool_fallback": True,
        "reason": "upstream returned text-only <function=...> tool call markup without native protocol tool_calls/tool_use",
        "calls": [
            {
                "id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
                "success": result.success,
                "failure_type": result.failure_type,
                "content": result.content,
            }
            for call, result in zip(calls, results)
        ],
    }
    report_text = (
        "Gateway 已识别并执行上游文本形式的工具调用。请基于这些真实工具结果继续分析；"
        "如果还需要工具，请优先返回原生 tool_calls/tool_use，不能支持时才继续使用 <function=...> 形式。\n\n"
        + json.dumps(tool_report, ensure_ascii=False, indent=2)
    )
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        messages.append(_assistant_message_from_chat_response(response))
        messages.append({"role": "user", "content": report_text})
        updated["messages"] = messages
        return updated
    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        text = _response_text(path, response)
        if text:
            messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": report_text})
        updated["messages"] = messages
        return updated
    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        input_items.append({"role": "assistant", "content": _response_text(path, response)})
        input_items.append({"role": "user", "content": report_text})
        updated["input"] = input_items
        return updated
    return updated


def _extract_mentioned_paths(text: str) -> list[str]:
    candidates = re.findall(r"@([A-Za-z0-9_./\\-]+)", text)
    out: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().strip(".,;:，。；：）)]}")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _should_build_local_planner_context(path: str, body: Json) -> bool:
    gateway = _gateway_config()
    if not gateway.get("local_planner_enabled", True):
        return False
    if path not in SUPPORTED_PATHS:
        return False
    text = _last_user_text(path, body)
    if not text:
        return False
    lowered = text.lower()
    analyze_intent = any(token in lowered for token in ("分析", "analyze", "review", "理解", "梳理"))
    code_scope = any(token in lowered for token in ("代码", "code", "项目", "project", "src", ".py", "class", "类", "@"))
    return analyze_intent and code_scope


def _select_local_planner_files(user_text: str, max_files: int) -> list[str]:
    roots = _extract_mentioned_paths(user_text)
    if not roots:
        roots = ["src", "README.md", "docs"]
    files: list[str] = []
    patterns_by_root: list[tuple[str, str]] = []
    for root in roots:
        normalized = root.rstrip("/") or "."
        try:
            resolved = _resolve_workspace_path(normalized)
        except Exception:
            continue
        if resolved.is_file():
            try:
                rel = str(resolved.relative_to(_workspace_root()))
                files.append(rel)
            except Exception:
                pass
        elif resolved.is_dir():
            if normalized.lower().endswith("docs"):
                patterns_by_root.append((normalized, "**/*.md"))
            elif normalized.lower().endswith("src") or "src" in normalized.lower():
                patterns_by_root.append((normalized, "**/*.py"))
            else:
                patterns_by_root.extend([(normalized, "**/*.py"), (normalized, "**/*.md")])
    for root, pattern in patterns_by_root:
        result = _execute_tool_call(ToolCall(f"planner_glob_{uuid.uuid4().hex}", "Glob", {"path": root, "pattern": pattern, "limit": max_files}, {}))
        if result.success:
            for line in result.content.splitlines():
                item = line.rstrip("/")
                if item and item not in files:
                    files.append(item)
                if len(files) >= max_files:
                    break
        if len(files) >= max_files:
            break
    return files[:max_files]


def _build_local_planner_context(user_text: str) -> str:
    gateway = _gateway_config()
    max_files = max(1, min(int(gateway.get("local_planner_max_files") or 24), 80))
    max_bytes = max(1000, min(int(gateway.get("local_planner_max_bytes_per_file") or 24000), 200000))
    sections: list[str] = []
    tree = _execute_tool_call(ToolCall(f"planner_tree_{uuid.uuid4().hex}", "Tree", {"path": ".", "max_depth": 3, "max_entries": 300}, {}))
    if tree.success:
        sections.append("## 本地工具结果：项目结构 Tree\n" + tree.content)
    files = _select_local_planner_files(user_text, max_files)
    if files:
        sections.append("## 本地工具结果：命中文件列表\n" + "\n".join(files))
    symbol_sections: list[str] = []
    for file_path in [f for f in files if f.endswith(".py")][:max_files]:
        symbols = _execute_tool_call(ToolCall(f"planner_symbols_{uuid.uuid4().hex}", "PythonSymbols", {"file_path": file_path}, {}))
        if symbols.success:
            symbol_sections.append(f"### {file_path}\n{symbols.content[:12000]}")
    if symbol_sections:
        sections.append("## 本地工具结果：Python 符号/类/函数\n" + "\n\n".join(symbol_sections))
    if files:
        read_many = _execute_tool_call(
            ToolCall(
                f"planner_read_{uuid.uuid4().hex}",
                "ReadManyFiles",
                {"paths": files, "max_files": max_files, "max_bytes_per_file": max_bytes},
                {},
            )
        )
        if read_many.success:
            sections.append("## 本地工具结果：关键文件内容\n" + read_many.content)
    return "\n\n".join(sections)


def _apply_local_planner_context(path: str, body: Json) -> Json:
    if not _should_build_local_planner_context(path, body):
        return body
    user_text = _last_user_text(path, body)
    context = _build_local_planner_context(user_text)
    if not context.strip():
        return body
    prompt = (
        "Gateway 已经在本地真实执行文件/符号/目录工具完成预分析。"
        "下面的工具结果是事实证据，不是提示词伪造的 tool call。"
        "请基于这些证据完成用户请求；如果证据不足，说明还需要哪些文件/工具。\n\n"
        "# 用户原始请求\n"
        f"{user_text}\n\n"
        "# Gateway 本地真实工具证据\n"
        f"{context}\n\n"
        "# 输出要求\n"
        "按 语义分析 / 逐个类或文件分析 / 调用与证据检查 / 反思调整 / 最终结论 输出。"
    )
    updated = _replace_last_user_text(path, body, prompt)
    updated.setdefault("gateway_context", {})
    updated["gateway_context"].update({"local_planner": True, "planner_evidence_chars": len(context)})
    return updated


def _failure_log_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("GATEWAY_TOOL_FAILURE_LOG") or ".gateway_tool_failures.jsonl")


def _logging_backend() -> str:
    backend = str(_gateway_config().get("logging_backend") or os.environ.get("GATEWAY_LOGGING_BACKEND") or "sqlite").lower()
    if backend == "sqlite":
        return "sqlite"
    # High-frequency gateway logs must not fall back to JSON/JSONL files unless
    # explicitly enabled for a one-off legacy/debug run. Legacy files are still
    # imported/read, but normal runtime writes stay in SQLite WAL.
    if os.environ.get("GATEWAY_ALLOW_FILE_LOGGING", "0").lower() not in {"1", "true", "yes"}:
        return "sqlite"
    return backend


def _sqlite_insert_tool_failure(event: Json) -> None:
    _sqlite_init()
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            conn.execute(
                """
                INSERT INTO tool_failures
                (ts, tool_name, call_id, failure_type, arguments_keys_json, content, fake_prompt_tools, execution_ms, retry_count, provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["ts"],
                    event["tool_name"],
                    event["call_id"],
                    event.get("failure_type"),
                    json.dumps(event.get("arguments_keys") or [], ensure_ascii=False),
                    event.get("content") or "",
                    1 if event.get("fake_prompt_tools") else 0,
                    event.get("execution_ms"),
                    event.get("retry_count") or 0,
                    event.get("provider"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _sqlite_record_tool_stat(name: str, success: bool, failure_type: str | None = None) -> None:
    _sqlite_init()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            row = conn.execute("SELECT calls, success, failure, failures_json FROM tool_stats WHERE tool_name = ?", (name,)).fetchone()
            if row:
                calls, ok_count, fail_count, failures_raw = row
                failures = json.loads(failures_raw or "{}")
            else:
                calls = ok_count = fail_count = 0
                failures = {}
            calls += 1
            if success:
                ok_count += 1
            else:
                fail_count += 1
                key = failure_type or "unknown"
                failures[key] = failures.get(key, 0) + 1
            conn.execute(
                """
                INSERT INTO tool_stats(tool_name, calls, success, failure, failures_json, last_called_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_name) DO UPDATE SET
                    calls=excluded.calls,
                    success=excluded.success,
                    failure=excluded.failure,
                    failures_json=excluded.failures_json,
                    last_called_at=excluded.last_called_at
                """,
                (name, calls, ok_count, fail_count, json.dumps(failures, ensure_ascii=False), now),
            )
            conn.commit()
        finally:
            conn.close()


def _sqlite_record_request_stat(path: str, status: int) -> None:
    _sqlite_init()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    status_key = str(status)
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            conn.execute("INSERT INTO request_stats(key, value) VALUES ('total', 1) ON CONFLICT(key) DO UPDATE SET value=value+1")
            conn.execute("INSERT INTO request_stats(key, value) VALUES ('last_request_at_epoch', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (int(time.time()),))
            conn.execute("INSERT INTO request_stats(key, value) VALUES ('last_request_at_iso', 0) ON CONFLICT(key) DO UPDATE SET value=0")
            conn.execute("INSERT INTO request_stats_by_path(path, value) VALUES (?, 1) ON CONFLICT(path) DO UPDATE SET value=value+1", (path,))
            conn.execute("INSERT INTO request_stats_by_status(status, value) VALUES (?, 1) ON CONFLICT(status) DO UPDATE SET value=value+1", (status_key,))
            conn.commit()
        finally:
            conn.close()


def _sqlite_insert_request_log(event: Json) -> None:
    _sqlite_init()
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            conn.execute(
                """
                INSERT INTO request_logs
                (ts, request_id, path, status, downstream_key, request_json, response_json, fake_prompt_tools)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["ts"],
                    event["request_id"],
                    event["path"],
                    int(event["status"]),
                    event.get("downstream_key"),
                    json.dumps(event.get("request") or {}, ensure_ascii=False),
                    json.dumps(event.get("response"), ensure_ascii=False) if event.get("response") is not None else None,
                    1 if event.get("fake_prompt_tools") else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _sqlite_stats_snapshot() -> Json:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        tools: Json = {}
        for name, calls, success, failure, failures_raw, last_called_at in conn.execute(
            "SELECT tool_name, calls, success, failure, failures_json, last_called_at FROM tool_stats ORDER BY tool_name"
        ):
            tools[name] = {
                "calls": calls,
                "success": success,
                "failure": failure,
                "failures": json.loads(failures_raw or "{}"),
                "last_called_at": last_called_at,
            }
        total_row = conn.execute("SELECT value FROM request_stats WHERE key='total'").fetchone()
        last_ts = conn.execute("SELECT ts FROM request_logs ORDER BY id DESC LIMIT 1").fetchone()
        by_path = {path: value for path, value in conn.execute("SELECT path, value FROM request_stats_by_path")}
        by_status = {status: value for status, value in conn.execute("SELECT status, value FROM request_stats_by_status")}
        memory_total_row = conn.execute("SELECT COUNT(*) FROM conversation_memories").fetchone()
        return {
            "tools": tools,
            "memory": {"total": int(memory_total_row[0]) if memory_total_row else 0},
            "requests": {
                "total": int(total_row[0]) if total_row else 0,
                "by_path": by_path,
                "by_status": by_status,
                "last_request_at": last_ts[0] if last_ts else None,
            },
            "backend": "sqlite",
            "sqlite_path": str(_sqlite_path()),
        }
    finally:
        conn.close()


def _sqlite_tail_requests(limit: int = 50) -> list[Json]:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        rows = conn.execute(
            "SELECT ts, request_id, path, status, downstream_key, request_json, response_json, fake_prompt_tools FROM request_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for ts, request_id, path, status, downstream_key, request_raw, response_raw, fake_prompt_tools in reversed(rows):
        out.append(
            {
                "ts": ts,
                "request_id": request_id,
                "path": path,
                "status": status,
                "downstream_key": downstream_key,
                "request": json.loads(request_raw or "{}"),
                "response": json.loads(response_raw) if response_raw else None,
                "fake_prompt_tools": bool(fake_prompt_tools),
            }
        )
    return out


def _sqlite_tail_failures(limit: int = 50) -> list[Json]:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        rows = conn.execute(
            "SELECT ts, tool_name, call_id, failure_type, arguments_keys_json, content, fake_prompt_tools FROM tool_failures ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for ts, tool_name, call_id, failure_type, keys_raw, content, fake_prompt_tools in reversed(rows):
        out.append(
            {
                "ts": ts,
                "tool_name": tool_name,
                "call_id": call_id,
                "failure_type": failure_type,
                "arguments_keys": json.loads(keys_raw or "[]"),
                "content": content,
                "fake_prompt_tools": bool(fake_prompt_tools),
            }
        )
    return out


def _record_tool_failure(
    call: ToolCall,
    result: ToolResult,
    *,
    execution_ms: float | None = None,
    retry_count: int | None = None,
    provider: str | None = None,
) -> None:
    if result.success:
        return
    if not _gateway_config().get("record_unsupported_tools", True):
        return
    event: dict = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "tool_name": call.name,
        "call_id": call.call_id,
        "failure_type": result.failure_type,
        "arguments_keys": sorted(call.arguments.keys()),
        "content": result.content[:1000] if result.content else "",
        "fake_prompt_tools": False,
    }
    if execution_ms is not None:
        event["execution_ms"] = execution_ms
    if retry_count is not None:
        event["retry_count"] = retry_count
    if provider is not None:
        event["provider"] = provider
    try:
        if _logging_backend() == "sqlite":
            _sqlite_insert_tool_failure(event)
        else:
            with _failure_log_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        if os.environ.get("DEBUG"):
            traceback.print_exc()


def _read_json_file(path: pathlib.Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        if os.environ.get("DEBUG"):
            traceback.print_exc()
    return copy.deepcopy(default)


def _write_json_file(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_tool_stat(name: str, success: bool, failure_type: str | None = None) -> None:
    if _logging_backend() == "sqlite":
        _sqlite_record_tool_stat(name, success, failure_type)
        return
    stats = _read_json_file(STATS_PATH, {"tools": {}, "requests": {"total": 0}})
    tools = stats.setdefault("tools", {})
    item = tools.setdefault(name, {"calls": 0, "success": 0, "failure": 0, "failures": {}})
    item["calls"] += 1
    if success:
        item["success"] += 1
    else:
        item["failure"] += 1
        failures = item.setdefault("failures", {})
        failures[failure_type or "unknown"] = failures.get(failure_type or "unknown", 0) + 1
    item["last_called_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _write_json_file(STATS_PATH, stats)


def _record_request_stat(path: str, status: int) -> None:
    if _logging_backend() == "sqlite":
        _sqlite_record_request_stat(path, status)
        return
    stats = _read_json_file(STATS_PATH, {"tools": {}, "requests": {"total": 0}})
    requests = stats.setdefault("requests", {"total": 0})
    requests["total"] = requests.get("total", 0) + 1
    by_path = requests.setdefault("by_path", {})
    by_path[path] = by_path.get(path, 0) + 1
    by_status = requests.setdefault("by_status", {})
    status_key = str(status)
    by_status[status_key] = by_status.get(status_key, 0) + 1
    requests["last_request_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _write_json_file(STATS_PATH, stats)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if key.lower() in {"authorization", "api_key", "x-api-key", "key", "token", "password", "secret"}:
                out[key] = "***"
            else:
                out[key] = _redact_payload(val)
        return out
    if isinstance(value, list):
        return [_redact_payload(v) for v in value]
    return value


def _write_request_log(path: str, body: Json, status: int, response: Json | None, downstream_key: str | None) -> None:
    if not load_config().get("gateway", {}).get("request_logging", True):
        return
    event = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "request_id": f"req_{uuid.uuid4().hex}",
        "path": path,
        "status": status,
        "downstream_key": downstream_key,
        "request": _redact_payload(body),
        "response": _redact_payload(response) if response is not None else None,
        "fake_prompt_tools": False,
    }
    if _logging_backend() == "sqlite":
        _sqlite_insert_request_log(event)
    else:
        with REQUEST_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _tail_jsonl(path: pathlib.Path, limit: int = 50) -> list[Json]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    rows = []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows


def _stats_snapshot() -> Json:
    if _logging_backend() == "sqlite":
        return _sqlite_stats_snapshot()
    return _read_json_file(STATS_PATH, {"tools": {}, "requests": {}})


def _tail_requests(limit: int = 50) -> list[Json]:
    if _logging_backend() == "sqlite":
        return _sqlite_tail_requests(limit)
    return _tail_jsonl(REQUEST_LOG_PATH, limit)


def _tail_failures(limit: int = 50) -> list[Json]:
    if _logging_backend() == "sqlite":
        return _sqlite_tail_failures(limit)
    return _tail_jsonl(_failure_log_path(), limit)


def _tool_catalog_snapshot() -> Json:
    unique: dict[str, GatewayTool] = {}
    aliases: dict[str, list[str]] = {}
    for public_name, tool in BUILTIN_TOOLS.items():
        unique.setdefault(tool.name, tool)
        if public_name != tool.name:
            aliases.setdefault(tool.name, []).append(public_name)
    tools = [
        {
            "name": tool.name,
            "aliases": sorted(set(aliases.get(tool.name, []))),
            "description": tool.description,
            "risk": tool.risk,
            "status": "connector_required" if tool.risk == "connector_required" else "ready",
            "parameters": tool.parameters,
        }
        for tool in sorted(unique.values(), key=lambda item: item.name.lower())
    ]
    failures = _tail_failures(500)
    failure_counts: dict[str, Json] = {}
    for failure in failures:
        name = str(failure.get("tool_name") or failure.get("tool") or "unknown")
        row = failure_counts.setdefault(name, {"tool": name, "count": 0, "failure_types": {}})
        row["count"] += 1
        failure_type = str(failure.get("failure_type") or "unknown")
        row["failure_types"][failure_type] = row["failure_types"].get(failure_type, 0) + 1
    unsupported = sorted(failure_counts.values(), key=lambda item: int(item["count"]), reverse=True)
    return {"tools": tools, "unsupported_or_failed": unsupported}


def _text_response(handler: BaseHTTPRequestHandler, status: int, payload: str, content_type: str = "text/html; charset=utf-8") -> None:
    data = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception:
        return None


def _check_admin(handler: BaseHTTPRequestHandler) -> bool:
    cfg = load_config()
    parsed = _parse_basic_auth(handler.headers.get("authorization"))
    admin = cfg.get("admin", {})
    if parsed and parsed[0] == admin.get("username", "admin") and _hash_secret(parsed[1]) == admin.get("password_hash"):
        return True
    handler.send_response(401)
    handler.send_header("www-authenticate", 'Basic realm="Gateway Admin"')
    handler.send_header("content-type", "text/plain; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(b"admin authentication required")
    return False


def _check_downstream_key(handler: BaseHTTPRequestHandler) -> str | None:
    cfg = load_config()
    keys = cfg.get("downstream_keys") or []
    if not keys:
        return "no-key-configured"
    auth = handler.headers.get("authorization") or ""
    supplied = ""
    if auth.startswith("Bearer "):
        supplied = auth.split(" ", 1)[1].strip()
    elif handler.headers.get("x-api-key"):
        supplied = handler.headers.get("x-api-key", "").strip()
    if not supplied:
        raise DownstreamAuthError("missing downstream API key")
    supplied_hash = _hash_secret(supplied)
    path = handler.path.split("?", 1)[0]
    protocol_by_path = {
        "/v1/chat/completions": "chat_completions",
        "/v1/responses": "responses",
        "/v1/messages": "messages",
        "/v1/messages/count_tokens": "messages",
        "/v1/tools/call": "direct_tools",
        "/v1/functions/call": "direct_tools",
        "/tools/call": "direct_tools",
        "/v1/models": "models",
    }
    requested_protocol = protocol_by_path.get(path)
    for item in keys:
        if item.get("enabled", True) and item.get("key_hash") == supplied_hash:
            allowed = item.get("protocols")
            if isinstance(allowed, list) and requested_protocol and requested_protocol not in allowed and "all" not in allowed:
                # Backward compatibility: older configs created before per-key protocol
                # support did not list `models`; allow model-list discovery for keys
                # that can call at least one conversation protocol.
                if not (
                    requested_protocol == "models"
                    and any(proto in allowed for proto in ("chat_completions", "responses", "messages"))
                ):
                    raise DownstreamAuthError(f"downstream key is not allowed to use protocol: {requested_protocol}")
            return str(item.get("name") or item.get("prefix") or "key")
    raise DownstreamAuthError("invalid downstream API key")


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("content-length") or "0")
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {k: v[-1] if v else "" for k, v in parsed.items()}


def _client_snippet_context() -> Json:
    cfg = load_config()
    gateway = cfg.get("gateway", {}) if isinstance(cfg.get("gateway"), dict) else {}
    upstream = cfg.get("upstream", {}) if isinstance(cfg.get("upstream"), dict) else {}
    base_url = str(gateway.get("public_base_url") or os.environ.get("GATEWAY_PUBLIC_BASE_URL") or "http://127.0.0.1:8885").rstrip("/")
    api_key = str(gateway.get("client_snippet_api_key") or os.environ.get("DOWNSTREAM_API_KEY") or os.environ.get("GATEWAY_DOWNSTREAM_KEY") or "")
    model = str(gateway.get("downstream_model_alias") or upstream.get("model") or os.environ.get("UPSTREAM_MODEL") or "mimo-v2.5-pro")
    review_model = str(gateway.get("review_model_alias") or model)
    context_window = int(gateway.get("client_context_window") or 1_000_000)
    auto_compact = int(gateway.get("client_auto_compact_token_limit") or max(context_window - 100_000, 1_000))
    output_limit = int(gateway.get("client_output_token_limit") or upstream.get("max_output_tokens") or 128_000)
    reasoning_effort = str(gateway.get("codex_reasoning_effort") or "xhigh")
    return {
        "base_url": base_url,
        "base_url_v1": f"{base_url}/v1",
        "api_key": api_key,
        "model": model,
        "review_model": review_model,
        "context_window": context_window,
        "auto_compact_token_limit": auto_compact,
        "output_token_limit": output_limit,
        "reasoning_effort": reasoning_effort,
    }


def _toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _client_config_snippets() -> Json:
    c = _client_snippet_context()
    codex_config_toml = "\n".join(
        [
            'model_provider = "Gateway"',
            f"model = {_toml_string(c['model'])}",
            f"review_model = {_toml_string(c['review_model'])}",
            f"model_reasoning_effort = {_toml_string(c['reasoning_effort'])}",
            "disable_response_storage = true",
            'network_access = "enabled"',
            "windows_wsl_setup_acknowledged = true",
            f"model_context_window = {int(c['context_window'])}",
            f"model_auto_compact_token_limit = {int(c['auto_compact_token_limit'])}",
            "",
            "[model_providers.Gateway]",
            'name = "Gateway"',
            f"base_url = {_toml_string(c['base_url'])}",
            'wire_api = "responses"',
            "requires_openai_auth = true",
            "",
        ]
    )
    codex_auth_json = json.dumps({"OPENAI_API_KEY": c["api_key"]}, ensure_ascii=False, indent=2)
    opencode_json = json.dumps(
        {
            "provider": {
                "openai": {
                    "options": {
                        "baseURL": c["base_url_v1"],
                        "apiKey": c["api_key"],
                    },
                    "models": {
                        c["model"]: {
                            "name": c["model"],
                            "limit": {
                                "context": c["context_window"],
                                "output": c["output_token_limit"],
                            },
                            "options": {"store": False},
                            "variants": {"low": {}, "medium": {}, "high": {}, "xhigh": {}},
                        }
                    },
                }
            },
            "agent": {
                "build": {"options": {"store": False}},
                "plan": {"options": {"store": False}},
            },
            "$schema": "https://opencode.ai/config.json",
        },
        ensure_ascii=False,
        indent=2,
    )
    claude_bash_profile_function = "\n".join(
        [
            "claude_m1() {",
            f'    export ANTHROPIC_BASE_URL="{c["base_url"]}"',
            "    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            f'    export ANTHROPIC_AUTH_TOKEN="{c["api_key"]}"',
            '    export ANTHROPIC_API_KEY=""',
            f'    export ANTHROPIC_DEFAULT_OPUS_MODEL="{c["model"]}"',
            f'    export ANTHROPIC_DEFAULT_SONNET_MODEL="{c["model"]}"',
            f'    export ANTHROPIC_DEFAULT_HAIKU_MODEL="{c["model"]}"',
            f'    export ANTHROPIC_MODEL="{c["model"]}"',
            f'    export ANTHROPIC_SMALL_FAST_MODEL="{c["model"]}"',
            '    export ENABLE_LSP_TOOL="1"',
            '    /usr/local/bin/claude --dangerously-skip-permissions "$@"',
            "}",
        ]
    )
    claude_terminal_env = "\n".join(
        [
            f'export ANTHROPIC_BASE_URL="{c["base_url"]}"',
            f'export ANTHROPIC_AUTH_TOKEN="{c["api_key"]}"',
            "export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            "export CLAUDE_CODE_ATTRIBUTION_HEADER=0",
        ]
    )
    vscode_claude_settings_json = json.dumps(
        {
            "env": {
                "ANTHROPIC_BASE_URL": c["base_url"],
                "ANTHROPIC_AUTH_TOKEN": c["api_key"],
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            }
        },
        ensure_ascii=False,
        indent=2,
    )
    return {
        "context": c,
        "codex_config_toml": codex_config_toml,
        "codex_auth_json": codex_auth_json,
        "opencode_json": opencode_json,
        "claude_bash_profile_function": claude_bash_profile_function,
        "claude_terminal_env": claude_terminal_env,
        "vscode_claude_settings_json": vscode_claude_settings_json,
    }


def _render_client_config_ui() -> str:
    snippets = _client_config_snippets()
    c = snippets["context"]
    cards = [
        ("~/.codex/config.toml", snippets["codex_config_toml"]),
        ("~/.codex/auth.json", snippets["codex_auth_json"]),
        ("opencode.json", snippets["opencode_json"]),
        ("~/.bash_profile: claude_m1", snippets["claude_bash_profile_function"]),
        ("Terminal env", snippets["claude_terminal_env"]),
        ("~/.claude/settings.json", snippets["vscode_claude_settings_json"]),
    ]
    rendered_cards = "\n".join(
        f'<section><h2>{html.escape(title)}</h2><textarea rows="14" readonly>{html.escape(text)}</textarea></section>'
        for title, text in cards
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gateway Client Config</title>
<style>
body{{font-family:system-ui;margin:24px;max-width:1200px}}
input,textarea{{width:100%;box-sizing:border-box;margin:4px 0 10px;padding:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
section{{margin:18px 0;padding:14px;border:1px solid #ddd;border-radius:10px}}
a{{margin-right:12px}} button{{padding:8px 14px}}
</style></head><body>
<h1>Gateway Client Config / 下游客户端配置</h1>
<p><a href="/ui">Admin</a><a href="/client-config.json">JSON</a><a href="/healthz">Health</a></p>
<p>只生成配置片段，不自动写入 <code>~/.codex</code>、<code>~/.claude</code> 或 <code>.bash_profile</code>，避免破坏你现有环境。</p>
<form method="post" action="/admin/client-config">
<h2>生成参数</h2>
<label>Gateway Public Base URL</label><input name="public_base_url" value="{html.escape(c['base_url'])}">
<label>Downstream API Key</label><input name="client_snippet_api_key" value="{html.escape(c['api_key'])}">
<label>Model</label><input name="downstream_model_alias" value="{html.escape(c['model'])}">
<label>Review Model</label><input name="review_model_alias" value="{html.escape(c['review_model'])}">
<label>Codex Reasoning Effort</label><input name="codex_reasoning_effort" value="{html.escape(c['reasoning_effort'])}">
<label>Client Context Window</label><input name="client_context_window" value="{int(c['context_window'])}">
<label>Auto Compact Token Limit</label><input name="client_auto_compact_token_limit" value="{int(c['auto_compact_token_limit'])}">
<label>Output Token Limit</label><input name="client_output_token_limit" value="{int(c['output_token_limit'])}">
<button>保存并刷新配置片段</button>
</form>
{rendered_cards}
</body></html>"""


def _render_admin_ui() -> str:
    cfg = load_config()
    redacted = _redacted_config(cfg)
    stats = _stats_snapshot()
    failures = _tail_failures(20)
    requests = _tail_requests(20)
    upstream = cfg.get("upstream", {})
    gateway = cfg.get("gateway", {})
    capabilities = upstream.get("capabilities", {}) if isinstance(upstream.get("capabilities"), dict) else {}
    upstream_paths = upstream.get("paths", {}) if isinstance(upstream.get("paths"), dict) else {}
    context = cfg.get("context", {}) if isinstance(cfg.get("context"), dict) else {}
    tool_rows = "\n".join(
        f"<tr><td>{html.escape(name)}</td><td>{item.get('calls', 0)}</td><td>{item.get('success', 0)}</td><td>{item.get('failure', 0)}</td><td><code>{html.escape(json.dumps(item.get('failures', {}), ensure_ascii=False))}</code></td></tr>"
        for name, item in sorted((stats.get("tools") or {}).items())
    )
    key_rows = "\n".join(
        f"<tr><td>{html.escape(str(k.get('name')))}</td><td>{html.escape(str(k.get('prefix')))}</td><td>{'yes' if k.get('enabled', True) else 'no'}</td><td>{html.escape(','.join(k.get('protocols') or ['chat_completions','responses','messages','direct_tools']))}</td></tr>"
        for k in cfg.get("downstream_keys", [])
    )
    failure_rows = "\n".join(
        f"<tr><td>{html.escape(str(x.get('ts')))}</td><td>{html.escape(str(x.get('tool_name')))}</td><td>{html.escape(str(x.get('failure_type')))}</td><td><code>{html.escape(str(x.get('content')))}</code></td></tr>"
        for x in failures
    )
    request_rows = "\n".join(
        f"<tr><td>{html.escape(str(x.get('ts')))}</td><td>{html.escape(str(x.get('path')))}</td><td>{x.get('status')}</td><td>{html.escape(str(x.get('downstream_key')))}</td></tr>"
        for x in requests
    )
    mcp_json = html.escape(json.dumps(cfg.get("mcp", {}).get("servers", []), ensure_ascii=False, indent=2))
    http_actions_json = html.escape(json.dumps(cfg.get("http_actions", {}).get("actions", []), ensure_ascii=False, indent=2))
    mcp_session_count = len(MCP_SESSIONS)
    mcp_cache_count = len(MCP_TOOL_CATALOG_CACHE)
    mcp_health_rows = "\n".join(
        f"<tr><td>{html.escape(str(row.get('name')))}</td><td>{html.escape(str(row.get('status', 'unknown')))}</td><td>{html.escape(str(row.get('session')))}</td><td>{html.escape(str(row.get('cache')))}</td><td>{row.get('tool_count', row.get('cached_tool_count', 0))}</td><td><code>{html.escape(str(row.get('detail', '')))}</code></td></tr>"
        for row in _mcp_health_snapshot(probe=False)
    )
    upstream_profile_rows = "\n".join(
        f"<tr><td>{'✅' if str(profile.get('id')) == str(cfg.get('active_upstream')) else ''}</td><td><code>{html.escape(str(profile.get('id')))}</code></td><td>{html.escape(str(profile.get('name')))}</td><td>{html.escape(str(profile.get('protocol')))}</td><td>{html.escape(str(profile.get('model')))}</td><td>{html.escape(str(profile.get('base_url')))}</td><td><form method='post' action='/admin/upstream-profile' style='display:inline'><input type='hidden' name='profile_id' value='{html.escape(str(profile.get('id')))}'><button name='action' value='activate'>设为默认</button></form> <form method='post' action='/admin/upstream-profile' style='display:inline'><input type='hidden' name='profile_id' value='{html.escape(str(profile.get('id')))}'><button name='action' value='delete'>删除</button></form></td></tr>"
        for profile in cfg.get("upstream_profiles", []) if isinstance(profile, dict)
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gateway Admin</title>
<style>body{{font-family:system-ui;margin:24px;max-width:1200px}} input,select,textarea{{width:100%;box-sizing:border-box;margin:4px 0 10px;padding:8px}} table{{border-collapse:collapse;width:100%;margin:12px 0}} td,th{{border:1px solid #ddd;padding:6px;vertical-align:top}} code,pre{{background:#f6f6f6;padding:2px 4px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}} button{{padding:8px 14px}}</style>
</head><body>
<h1>Tool Call Gateway Admin</h1>
<p><a href="/client-config">下游客户端配置生成器</a> <a href="/client-config.json">Client config JSON</a> <a href="/healthz">Health</a></p>
<p>生产环境请通过环境变量 <code>GATEWAY_ADMIN_PASSWORD</code> 和 <code>GATEWAY_DOWNSTREAM_KEY</code> 配置管理员密码和下游 API Key。开发环境默认 admin/admin 和 local-gateway-key。</p>
<div class="grid">
<section><h2>上游 API</h2>
<p>支持添加多个上游 API；每个上游可以独立设置协议、路由、模型、streaming、tool call、识图、网络检索等能力。当前默认上游会用于所有下游协议请求。</p>
<table><tr><th>Active</th><th>ID</th><th>Name</th><th>Protocol</th><th>Model</th><th>Base URL</th><th>Actions</th></tr>{upstream_profile_rows}</table>
<form method="post" action="/admin/config">
<h3>添加/编辑上游 API 详情</h3>
<label>Profile ID（新 ID 会新增；已有 ID 会更新）</label><input name="profile_id" value="{html.escape(str(upstream.get('id','default')))}">
<label>Profile Name</label><input name="profile_name" value="{html.escape(str(upstream.get('name','default')))}">
<label>Base URL</label><input name="base_url" value="{html.escape(str(upstream.get('base_url','')))}">
<label>API Key（留空则不修改）</label><input name="api_key" type="password" placeholder="keep unchanged">
<label>Model</label><input name="model" value="{html.escape(str(upstream.get('model','')))}">
<label>Timeout Seconds</label><input name="upstream_timeout_seconds" value="{html.escape(str(upstream.get('timeout_seconds', 60)))}">
<label>Max Input Tokens</label><input name="upstream_max_input_tokens" value="{html.escape(str(upstream.get('max_input_tokens', 128000)))}">
<label>Max Output Tokens</label><input name="upstream_max_output_tokens" value="{html.escape(str(upstream.get('max_output_tokens', 8192)))}">
<label>Upstream Max Concurrency</label><input name="upstream_max_concurrency" value="{html.escape(str(upstream.get('max_concurrency', 32)))}">
<label>Protocol</label><select name="protocol">
{''.join(f'<option value="{p}" {"selected" if upstream.get("protocol") == p else ""}>{p}</option>' for p in ["openai_chat","openai_responses","anthropic_messages","openai_compatible"])}
</select>
<p><b>协议转换说明：</b>当上游 Protocol 选择 <code>openai_chat</code> / <code>openai_compatible</code> 时，下游仍可同时调用 <code>/v1/chat/completions</code>、<code>/v1/responses</code>、<code>/v1/messages</code>；Gateway 会把三种请求统一转换为上游 <code>Chat Completions Path</code>，再把返回转换回下游协议。</p>
<label>Tools Enabled</label><select name="tools_enabled">
{''.join(f'<option value="{p}" {"selected" if upstream.get("tools_enabled") == p else ""}>{p}</option>' for p in ["auto","on","off","native_only"])}
</select>
<label><input type="checkbox" name="native_tools_verified" value="1" {"checked" if upstream.get("native_tools_verified") else ""} style="width:auto"> Native tools 已验证</label>
<label><input type="checkbox" name="use_for_coding" value="1" {"checked" if upstream.get("use_for_coding", True) else ""} style="width:auto"> 用于 coding agent</label>
<h3>Upstream Capabilities / 能力开关</h3>
<label><input type="checkbox" name="cap_supports_streaming" value="1" {"checked" if capabilities.get("supports_streaming", True) else ""} style="width:auto"> 支持 streaming</label>
<label><input type="checkbox" name="cap_supports_tools" value="1" {"checked" if capabilities.get("supports_tools", True) else ""} style="width:auto"> 支持 tool calls</label>
<label><input type="checkbox" name="cap_supports_function_calls" value="1" {"checked" if capabilities.get("supports_function_calls", True) else ""} style="width:auto"> 支持 function calls</label>
<label><input type="checkbox" name="cap_supports_parallel_tool_calls" value="1" {"checked" if capabilities.get("supports_parallel_tool_calls", True) else ""} style="width:auto"> 支持 parallel tool calls</label>
<label><input type="checkbox" name="cap_supports_vision" value="1" {"checked" if capabilities.get("supports_vision") else ""} style="width:auto"> 支持识图 / vision</label>
<label><input type="checkbox" name="cap_supports_network" value="1" {"checked" if capabilities.get("supports_network") else ""} style="width:auto"> 支持网络 / web</label>
<label><input type="checkbox" name="cap_supports_web_search" value="1" {"checked" if capabilities.get("supports_web_search") or capabilities.get("supports_network") else ""} style="width:auto"> 支持网络检索 / web search</label>
<label><input type="checkbox" name="cap_supports_json_schema" value="1" {"checked" if capabilities.get("supports_json_schema", True) else ""} style="width:auto"> 支持 JSON Schema / structured outputs</label>
<h3>Upstream Routes / 路由适配</h3>
<label>Models Path</label><input name="path_models" value="{html.escape(str(upstream_paths.get('models','/v1/models')))}">
<label>Chat Completions Path</label><input name="path_chat_completions" value="{html.escape(str(upstream_paths.get('chat_completions','/v1/chat/completions')))}">
<label>Responses Path</label><input name="path_responses" value="{html.escape(str(upstream_paths.get('responses','/v1/responses')))}">
<label>Messages Path</label><input name="path_messages" value="{html.escape(str(upstream_paths.get('messages','/v1/messages')))}">
	<h3>Gateway Runtime</h3>
	<label>Tool Mode</label><select name="tool_mode">
	{''.join(f'<option value="{p}" {"selected" if gateway.get("tool_mode") == p else ""}>{p}</option>' for p in ["orchestrate","passthrough"])}
	</select>
	<label>Max Tool Rounds</label><input name="max_tool_rounds" value="{html.escape(str(gateway.get('max_tool_rounds', DEFAULT_MAX_TOOL_ROUNDS)))}">
	<label>Max Concurrent Requests</label><input name="max_concurrent_requests" value="{html.escape(str(gateway.get('max_concurrent_requests', 32)))}">
	<label>Concurrency Queue Timeout Seconds</label><input name="concurrency_queue_timeout_seconds" value="{html.escape(str(gateway.get('concurrency_queue_timeout_seconds', 5)))}">
	<label>Tool Execution Timeout Seconds</label><input name="tool_execution_timeout_seconds" value="{html.escape(str(gateway.get('tool_execution_timeout_seconds', 60)))}">
	<label>Workspace Root</label><input name="workspace_root" value="{html.escape(str(gateway.get('workspace_root','')))}">
		<label><input type="checkbox" name="allow_write_tools" value="1" {"checked" if gateway.get("allow_write_tools") else ""} style="width:auto"> 允许写入工具</label>
		<label><input type="checkbox" name="allow_shell_tools" value="1" {"checked" if gateway.get("allow_shell_tools") else ""} style="width:auto"> 允许 Shell 工具</label>
	<label><input type="checkbox" name="request_logging" value="1" {"checked" if gateway.get("request_logging", True) else ""} style="width:auto"> 保留下游请求和响应</label>
		<label><input type="checkbox" name="record_unsupported_tools" value="1" {"checked" if gateway.get("record_unsupported_tools", True) else ""} style="width:auto"> 记录不支持/失败的 tools，方便后续增强</label>
		<label><input type="checkbox" name="text_tool_call_fallback_enabled" value="1" {"checked" if gateway.get("text_tool_call_fallback_enabled", True) else ""} style="width:auto"> 上游只输出文本 &lt;function=...&gt; 时，本地识别并执行工具</label>
		<h3>Context Router / 分流压缩</h3>
		<label><input type="checkbox" name="context_enabled" value="1" {"checked" if context.get("enabled") else ""} style="width:auto"> 启用上下文治理</label>
		<label><input type="checkbox" name="context_fanout_enabled" value="1" {"checked" if context.get("fanout_enabled") else ""} style="width:auto"> 超大请求 fan-out 分流分析后综合</label>
		<label><input type="checkbox" name="context_quality_review_enabled" value="1" {"checked" if context.get("quality_review_enabled", True) else ""} style="width:auto"> 分流综合后再做检查/反思/调整</label>
		<label>Max Input Tokens</label><input name="context_max_input_tokens" value="{html.escape(str(context.get('max_input_tokens', 24000)))}">
		<label>Fanout Chunk Tokens</label><input name="context_fanout_chunk_tokens" value="{html.escape(str(context.get('fanout_chunk_tokens', 12000)))}">
		<label>Fanout Max Chunks（0 = 不限制，按内容切完）</label><input name="context_fanout_max_chunks" value="{html.escape(str(context.get('fanout_max_chunks', 0)))}">
		<label>Fanout Max Workers</label><input name="context_fanout_max_workers" value="{html.escape(str(context.get('fanout_max_workers', 4)))}">
	<button>保存上游和运行配置</button>
</form></section>
<section><h2>下游 API Keys</h2>
<p>可添加多个下游 key；每个 key 默认可访问 Chat Completions / Responses / Anthropic Messages，Gateway 会按需要转换到当前上游协议。</p>
<table><tr><th>Name</th><th>Prefix</th><th>Enabled</th><th>Protocols</th></tr>{key_rows}</table>
<form method="post" action="/admin/downstream-key">
<label>Name</label><input name="name" placeholder="codex-local">
<label>Key</label><input name="key" placeholder="your-api-key">
<label><input type="checkbox" name="key_proto_models" value="1" checked style="width:auto"> /v1/models</label>
<label><input type="checkbox" name="key_proto_chat" value="1" checked style="width:auto"> /v1/chat/completions</label>
<label><input type="checkbox" name="key_proto_responses" value="1" checked style="width:auto"> /v1/responses</label>
<label><input type="checkbox" name="key_proto_messages" value="1" checked style="width:auto"> /v1/messages</label>
<label><input type="checkbox" name="key_proto_tools" value="1" checked style="width:auto"> direct tools/functions</label>
<button>添加/更新 Key</button>
</form>
<h2>修改管理员密码</h2>
<form method="post" action="/admin/password">
<label>New password</label><input type="password" name="password">
<button>修改密码</button>
</form></section>
</div>
<section><h2>本地 MCP / Connector Catalog</h2>
<form method="post" action="/admin/mcp">
<textarea name="servers" rows="8">{mcp_json}</textarea>
<button>保存 MCP 配置</button>
</form>
<form method="post" action="/admin/mcp-reload"><button>刷新 MCP 连接和工具缓存</button></form>
<p>当前已支持 stdio MCP <code>initialize</code> / <code>tools/list</code> / <code>tools/call</code>，ready tools 会以 <code>mcp__server__tool</code> 形式自动暴露，并兼容 DeepSeek-TUI 风格 <code>mcp_server_tool</code> 名称。</p>
<p>MCP sessions: <code>{mcp_session_count}</code>，catalog cache: <code>{mcp_cache_count}</code>。查看 <code>/admin/mcp-tools.json</code>。</p>
<table><tr><th>Server</th><th>Status</th><th>Session</th><th>Cache</th><th>Tools</th><th>Detail</th></tr>{mcp_health_rows}</table>
</section>
<section><h2>HTTP Actions</h2>
<form method="post" action="/admin/http-actions">
<textarea name="actions" rows="8">{http_actions_json}</textarea>
<button>保存 HTTP Actions</button>
</form>
<p>HTTP action 会作为真实 tool/function executor 暴露，默认直接使用 action <code>name</code>。POST/PUT/PATCH 会把工具参数作为 JSON body；GET/DELETE 会把参数放到 query。</p>
<p>示例：<code>{{"name":"lookup_user","method":"POST","url":"http://127.0.0.1:9000/lookup","input_schema":{{"type":"object","properties":{{"id":{{"type":"string"}}}}}}}}</code></p>
</section>
<section><h2>Tool 调用频次</h2><table><tr><th>Tool</th><th>Calls</th><th>Success</th><th>Failure</th><th>Failures</th></tr>{tool_rows}</table></section>
<section><h2>失败/不支持 Function Calls / Tool Calls</h2><table><tr><th>Time</th><th>Tool</th><th>Type</th><th>Content</th></tr>{failure_rows}</table><p>这些会进入 marketplace/backlog 搜索与后续实现。</p></section>
<section><h2>最近下游请求</h2><table><tr><th>Time</th><th>Path</th><th>Status</th><th>Key</th></tr>{request_rows}</table></section>
<section><h2>当前配置（脱敏）</h2><pre>{html.escape(json.dumps(redacted, ensure_ascii=False, indent=2))}</pre></section>
</body></html>"""


def _redirect(handler: BaseHTTPRequestHandler, location: str = "/ui") -> None:
    handler.send_response(303)
    handler.send_header("location", location)
    handler.end_headers()


def _execute_tool_call(call: ToolCall, provider: str | None = None) -> ToolResult:
    import time as _time
    _start = _time.time()
    original_name = call.name
    call = _normalize_tool_call(call)
    tool = BUILTIN_TOOLS.get(call.name)
    mcp_target = _mcp_parse_public_name(call.name)
    cfg = _gateway_config() if callable(_gateway_config) else _gateway_config
    max_retries = cfg.get("tool_max_retries", 1) if isinstance(cfg, dict) else 1
    provider = provider or "unknown"
    last_exc: Exception | None = None
    last_result: ToolResult | None = None
    for attempt in range(max_retries + 1):
        try:
            if mcp_target:
                server_name, mcp_tool_name = mcp_target
                server = _mcp_server_by_name(server_name)
                if not server:
                    result = ToolResult(
                        call_id=call.call_id, name=call.name,
                        content=f"connector_required: MCP server {server_name} is not configured or enabled",
                        success=False, failure_type="connector_required",
                    )
                    _record_tool_failure(call, result, execution_ms=_time.time()-_start, retry_count=attempt, provider=provider)
                    _record_tool_stat(call.name, False, "connector_required")
                    return result
                content = _mcp_call_tool(server, mcp_tool_name, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            http_action = _http_action_by_name(call.name) or _http_action_by_name(original_name)
            if http_action:
                content = _call_http_action(http_action, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            if not tool:
                result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"ToolNotFound: {call.name} is not implemented or installed in Gateway runtime",
                    success=False, failure_type="tool_not_found",
                )
                _record_tool_failure(call, result, execution_ms=_time.time()-_start, retry_count=attempt, provider=provider)
                _record_tool_stat(call.name, False, "tool_not_found")
                return result
            content = tool.handler(call.arguments)
            _record_tool_stat(call.name, True)
            return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
        except (ToolExecutionError, subprocess.TimeoutExpired, Exception) as exc:
            last_exc = exc
            if isinstance(exc, subprocess.TimeoutExpired):
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"timeout: tool execution exceeded {exc.timeout}s",
                    success=False, failure_type="timeout",
                )
            elif isinstance(exc, ToolExecutionError):
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"{exc.failure_type}: {exc}",
                    success=False, failure_type=exc.failure_type,
                )
            else:
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"execution_failed: {exc}",
                    success=False, failure_type="execution_failed",
                )
            # transient failure — retry if attempts remain
    # All attempts exhausted
    failure_type = getattr(last_exc, "failure_type", "execution_failed") if last_exc and isinstance(last_exc, ToolExecutionError) else getattr(last_result, "failure_type", "execution_failed") if last_result else "execution_failed"
    _record_tool_failure(call, last_result, execution_ms=_time.time()-_start, retry_count=max_retries, provider=provider)
    _record_tool_stat(call.name, False, failure_type)
    return last_result


def _direct_tool_result_payload(result: ToolResult) -> Json:
    payload: Json = {
        "id": result.call_id,
        "object": "gateway.tool_result",
        "name": result.name,
        "success": result.success,
        "failure_type": result.failure_type,
        "content": result.content,
        "fake_prompt_tools": False,
        "openai_chat": {
            "role": "tool",
            "tool_call_id": result.call_id,
            "content": result.content,
        },
        "openai_responses": {
            "type": "function_call_output",
            "call_id": result.call_id,
            "output": result.content,
        },
        "anthropic": {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": result.content,
            "is_error": not result.success,
        },
    }
    return payload


def execute_direct_tool_call(body: Json) -> Json:
    with _workspace_scope(_request_workspace_root(body)):
        calls = _direct_tool_calls_from_body(body)
        results = [_execute_tool_call(call, provider="direct") for call in calls]
    payloads = [_direct_tool_result_payload(result) for result in results]
    if len(payloads) == 1:
        return payloads[0]
    return {
        "object": "gateway.tool_results",
        "success": all(result.success for result in results),
        "results": payloads,
        "fake_prompt_tools": False,
    }



def _looks_like_context_rejection(text: str) -> bool:
    lowered = (text or "").lower()
    needles = (
        "text you sent is too long",
        "too long",
        "context length",
        "maximum context",
        "input is too large",
        "send it in parts",
        "simplify the content",
        "文本太长",
        "内容过长",
        "上下文",
        "分段发送",
    )
    return any(needle in lowered for needle in needles)

def token_count_response(body: Json) -> Json:
    return {"input_tokens": _body_token_estimate(body)}


def run_tool_orchestration(path: str, body: Json, client: NativeProxyClient | None = None) -> Json:
    with _workspace_scope(_request_workspace_root(body)):
        return _run_tool_orchestration_scoped(path, body, client)


def _run_tool_orchestration_scoped(path: str, body: Json, client: NativeProxyClient | None = None) -> Json:
    mode = _config_env("GATEWAY_TOOL_MODE", "orchestrate").lower()
    memory_body = _inject_recalled_memories(path, body)
    if mode in {"passthrough", "native_passthrough", "proxy"}:
        response = (client or NativeProxyClient()).forward(path, memory_body)
        _verify_native_if_forced(path, memory_body, response)
        _remember_conversation_turn(path, body, response)
        return response
    max_rounds = int(_config_env("GATEWAY_MAX_TOOL_ROUNDS", str(DEFAULT_MAX_TOOL_ROUNDS)))
    upstream = client or NativeProxyClient()
    context_cfg = _context_config()
    fanout_response = _run_context_fanout(path, memory_body, upstream, context_cfg)
    if fanout_response is not None:
        _remember_conversation_turn(path, body, fanout_response)
        return fanout_response
    request_body = _merge_builtin_tools(path, _apply_local_planner_context(path, _maybe_compact_request_for_upstream(path, memory_body, context_cfg)))
    for _round in range(max_rounds):
        response = upstream.forward(path, request_body)
        response_text = _response_text(path, response)
        if _looks_like_context_rejection(response_text):
            forced_fanout = _run_context_fanout(path, memory_body, upstream, context_cfg, force=True)
            if forced_fanout is not None:
                _remember_conversation_turn(path, body, forced_fanout)
                return forced_fanout
        _verify_native_if_forced(path, request_body, response)
        calls = _extract_tool_calls(path, response)
        text_fallback = False
        if not calls:
            calls = _extract_text_tool_calls(path, response)
            text_fallback = bool(calls)
        if not calls:
            _remember_conversation_turn(path, body, response)
            return response
        results = [_execute_tool_call(call) for call in calls]
        if text_fallback:
            request_body = _append_text_tool_results(path, request_body, response, calls, results)
        else:
            request_body = _append_tool_results(path, request_body, response, results)
    raise GatewayError("max tool rounds exceeded", detail={"max_tool_rounds": max_rounds})


def _error_payload(message: str, *, detail: Any | None = None, upstream_status: int | None = None) -> Json:
    payload: Json = {
        "error": {
            "message": message,
            "type": "native_tool_gateway_error",
            "fake_prompt_tools": False,
        }
    }
    if detail is not None:
        payload["error"]["detail"] = detail
    if upstream_status is not None:
        payload["error"]["upstream_status"] = upstream_status
    return payload


def _stream_mode_passthrough() -> bool:
    mode = _config_env("GATEWAY_TOOL_MODE", "orchestrate").lower()
    return mode in {"passthrough", "native_passthrough", "proxy"}


def _send_sse_headers(handler: BaseHTTPRequestHandler, status: int = 200) -> None:
    handler.send_response(status)
    handler.send_header("content-type", "text/event-stream; charset=utf-8")
    handler.send_header("cache-control", "no-cache")
    handler.send_header("connection", "close")
    handler.send_header("x-accel-buffering", "no")
    handler.end_headers()
    handler.close_connection = True


def _write_sse(handler: BaseHTTPRequestHandler, payload: Any, *, event: str | None = None) -> None:
    if event:
        handler.wfile.write(f"event: {event}\n".encode("utf-8"))
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for line in data.splitlines() or [""]:
        handler.wfile.write(f"data: {line}\n".encode("utf-8"))
    handler.wfile.write(b"\n")
    handler.wfile.flush()


def _stream_tool_start(handler: BaseHTTPRequestHandler, call_id: str, name: str) -> None:
    """Send SSE event when a tool call starts execution."""
    _write_sse(handler, {
        "type": "tool_start",
        "call_id": call_id,
        "name": name,
    }, event="tool_start")


def _stream_tool_progress(handler: BaseHTTPRequestHandler, call_id: str, name: str, progress: str) -> None:
    """Send SSE event for tool execution progress (for long-running tools)."""
    _write_sse(handler, {
        "type": "tool_progress",
        "call_id": call_id,
        "name": name,
        "progress": progress,
    }, event="tool_progress")


def _stream_tool_end(handler: BaseHTTPRequestHandler, call_id: str, name: str, success: bool, content: str) -> None:
    """Send SSE event when a tool call completes."""
    _write_sse(handler, {
        "type": "tool_end",
        "call_id": call_id,
        "name": name,
        "success": success,
        "content": content,
    }, event="tool_end")


def _stream_tool_error(handler: BaseHTTPRequestHandler, call_id: str, name: str, error: str) -> None:
    """Send SSE event when a tool call fails."""
    _write_sse(handler, {
        "type": "tool_error",
        "call_id": call_id,
        "name": name,
        "error": error,
    }, event="tool_error")


# ---------------------------------------------------------------------------
# Backward-compat re-exports
# Allow: from gateway_tool_runtime import run_tool_orchestration
# ---------------------------------------------------------------------------

# Re-exported from gateway_app (must be imported at runtime to avoid circular)
def __getattr__(name: str):
    _mod = None
    try:
        from . import gateway_app as _mod
    except ImportError:
        try:
            import gateway_app as _mod
        except ImportError:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if hasattr(_mod, name):
        return getattr(_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
