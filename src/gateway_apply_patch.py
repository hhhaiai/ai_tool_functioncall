#!/usr/bin/env python3
"""Small stdlib fallback for Codex-style apply_patch input.

The Gateway executes this helper only inside its validated temporary overlay.
It intentionally implements the core Add/Update/Delete/Move grammar without
accessing Gateway configuration or the original workspace.
"""
from __future__ import annotations

import pathlib
import sys


class PatchError(RuntimeError):
    pass


def _safe_path(raw: str) -> pathlib.Path:
    path = pathlib.Path(raw.strip())
    if not raw.strip() or path.is_absolute() or any(part == ".." for part in path.parts):
        raise PatchError(f"invalid patch path: {raw}")
    return path


def _find_subsequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return start
    matches = [
        index
        for index in range(max(0, start), len(lines) - len(needle) + 1)
        if lines[index:index + len(needle)] == needle
    ]
    if not matches:
        raise PatchError("update hunk context was not found")
    if len(matches) > 1:
        raise PatchError("update hunk context is ambiguous")
    return matches[0]


def _apply_update(source: str, patch_lines: list[str]) -> str:
    source_had_newline = source.endswith("\n")
    source_lines = source.splitlines()
    output: list[str] = []
    source_cursor = 0
    index = 0
    saw_hunk = False
    while index < len(patch_lines):
        header = patch_lines[index]
        if not header.startswith("@@"):
            raise PatchError(f"expected update hunk header, got: {header}")
        saw_hunk = True
        index += 1
        old: list[str] = []
        new: list[str] = []
        while index < len(patch_lines) and not patch_lines[index].startswith("@@"):
            line = patch_lines[index]
            index += 1
            if line == "\\ No newline at end of file":
                continue
            if not line:
                raise PatchError("hunk line is missing a prefix")
            prefix, text = line[0], line[1:]
            if prefix == " ":
                old.append(text)
                new.append(text)
            elif prefix == "-":
                old.append(text)
            elif prefix == "+":
                new.append(text)
            else:
                raise PatchError(f"unsupported hunk line prefix: {prefix}")
        position = _find_subsequence(source_lines, old, source_cursor)
        output.extend(source_lines[source_cursor:position])
        output.extend(new)
        source_cursor = position + len(old)
    if not saw_hunk:
        raise PatchError("update section contains no hunks")
    output.extend(source_lines[source_cursor:])
    rendered = "\n".join(output)
    if source_had_newline or rendered:
        rendered += "\n"
    return rendered


def apply_patch_text(patch: str) -> None:
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise PatchError("patch must start with '*** Begin Patch' and end with '*** End Patch'")
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        index += 1
        if header.startswith("*** Add File: "):
            path = _safe_path(header.removeprefix("*** Add File: "))
            if path.exists():
                raise PatchError(f"add target already exists: {path}")
            content: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                line = lines[index]
                index += 1
                if not line.startswith("+"):
                    raise PatchError("add-file content lines must start with '+'")
                content.append(line[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(content) + ("\n" if content else ""), encoding="utf-8")
            continue
        if header.startswith("*** Delete File: "):
            path = _safe_path(header.removeprefix("*** Delete File: "))
            if not path.is_file() or path.is_symlink():
                raise PatchError(f"delete target is not a regular file: {path}")
            path.unlink()
            continue
        if header.startswith("*** Update File: "):
            source_path = _safe_path(header.removeprefix("*** Update File: "))
            if not source_path.is_file() or source_path.is_symlink():
                raise PatchError(f"update target is not a regular file: {source_path}")
            move_path: pathlib.Path | None = None
            if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
                move_path = _safe_path(lines[index].removeprefix("*** Move to: "))
                index += 1
                if move_path.exists():
                    raise PatchError(f"move destination already exists: {move_path}")
            section: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                section.append(lines[index])
                index += 1
            updated = _apply_update(source_path.read_text(encoding="utf-8"), section)
            destination = move_path or source_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(updated, encoding="utf-8")
            if move_path is not None:
                source_path.unlink()
            continue
        raise PatchError(f"unsupported patch directive: {header}")


def main() -> int:
    try:
        apply_patch_text(sys.stdin.read())
    except Exception as exc:
        print(f"apply_patch fallback error: {exc}", file=sys.stderr)
        return 1
    print("Done!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
