#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check fenced YAML, TOML, JSON, Python and shell code examples in tracked Markdown files.

A shell example's <name> placeholders are read as the bare word name before
bash -n or sh -n; nothing is executed. A missing interpreter is a failure,
never a skip.

    examples.py [check] [--root DIR]

Exit codes: 0 pass, 1 parse failure, 3 file or git error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import deque
from pathlib import Path

try:
    from yaml import YAMLError, safe_load_all
except ImportError:
    safe_load_all = None
    YAMLError = Exception

FENCE_START = re.compile(r"^(\s{0,3})(`{3,}|~{3,})(.*)")
PLACEHOLDER = re.compile(r"<([A-Za-z][A-Za-z0-9_.:/-]*)>")
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z0-9_][A-Za-z0-9_.-]*)\2")
# Known limit: a case-pattern ')' inside $(...) ends the scanned substitution early.
SHELL_TOKEN = re.compile(
    HEREDOC.pattern + r"|['\"`()]|\\.|\$\(\(.*?\)\)|\$\((?!\()|(?P<comment>(?<![^\s;&|()])#)"
)
SHELL_SINGLE_QUOTED = re.compile(r"[^']*(?P<closed>')?")
SHELL_DOUBLE_QUOTED = re.compile(r'"|\\.|\$\(\(.*?\)\)|\$\((?!\()|`')
DEADLINE_SECONDS = 30
# Largest tracked fence: 670 characters on 2026-11-14.
MAX_EXAMPLE_CHARS = 100_000


def say(line: str) -> None:
    sys.stdout.write(line + "\n")


def complain(line: str) -> None:
    sys.stderr.write(line + "\n")


def fenced_blocks(text: str) -> list[tuple[int, str, str]]:
    """Extract fenced code blocks as (start_line, language, content)."""
    blocks: list[tuple[int, str, str]] = []
    in_fence, fence_char, fence_len, indent = False, "", 0, 0
    start_line, lang, current_lines = 0, "", []
    for line_num, line in enumerate(text.splitlines(), start=1):
        if not in_fence:
            m = FENCE_START.match(line)
            if m:
                indent = len(m.group(1))
                fence_char = m.group(2)[0]
                fence_len = len(m.group(2))
                info = m.group(3).strip().split()
                lang = info[0].lower() if info else ""
                start_line = line_num
                current_lines = []
                in_fence = True
        elif re.match(rf"^\s{{0,3}}{re.escape(fence_char)}{{{fence_len},}}\s*$", line):
            in_fence = False
            blocks.append((start_line, lang, "\n".join(current_lines)))
        else:
            if indent > 0 and line.startswith(" " * indent):
                current_lines.append(line[indent:])
            else:
                current_lines.append(line)
    if in_fence:
        blocks.append((start_line, lang, "\n".join(current_lines)))
    return blocks


def _check_yaml(content: str) -> str | None:
    if safe_load_all is None:
        return "PyYAML not installed"
    try:
        list(safe_load_all(content))
    except (YAMLError, ValueError) as err:
        return str(err)
    return None


def _check_toml(content: str) -> str | None:
    try:
        tomllib.loads(content)
    except (tomllib.TOMLDecodeError, ValueError) as err:
        return str(err)
    return None


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"{constant} is not JSON")


def _check_json(content: str) -> str | None:
    try:
        json.loads(content, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as err:
        return str(err)
    return None


def _check_python(content: str) -> str | None:
    try:
        compile(content, "<example>", "exec", dont_inherit=True)
    # CPython's parser uses MemoryError for source too complex, not other allocation failures.
    except (SyntaxError, ValueError, RecursionError, MemoryError) as err:
        return str(err)
    return None


def _shell_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}


def _shell_error(done: subprocess.CompletedProcess[str]) -> str | None:
    if done.returncode == 0:
        return None
    return next(
        (line for line in done.stderr.splitlines() if line.strip()), f"exit {done.returncode}"
    )


def _shell_code_frame(symbol: str, frames: list[str]) -> None:
    """Track quotes, substitutions and parentheses encountered in shell code."""
    if symbol in ("'", '"', "$(", "("):
        frames.append(symbol)
    elif symbol == "`":
        if frames and frames[-1] == "`":
            frames.pop()
        else:
            frames.append(symbol)
    elif symbol == ")" and frames and frames[-1] in ("$(", "("):
        frames.pop()


def _shell_quoted_frame(line: str, position: int, frames: list[str]) -> int:
    """Advance quoted text, entering command substitutions inside double quotes."""
    if frames[-1] == "'":
        match = SHELL_SINGLE_QUOTED.match(line, position)
        if match.group("closed"):
            frames.pop()
        return match.end()
    match = SHELL_DOUBLE_QUOTED.search(line, position)
    if match is None:
        return len(line)
    symbol = match.group()
    if symbol == '"':
        frames.pop()
    elif symbol in ("$(", "`"):
        frames.append(symbol)
    return match.end()


def _heredocs(line: str, frames: tuple[str, ...]) -> tuple[list[tuple[str, bool]], tuple[str, ...]]:
    """Find heredocs in shell code, including code inside quoted substitutions."""
    delimiters: list[tuple[str, bool]] = []
    stack = list(frames)
    position = 0
    for _ in range(len(line) + 1):
        if position >= len(line):
            break
        if stack and stack[-1] in ("'", '"'):
            position = _shell_quoted_frame(line, position, stack)
            continue
        match = SHELL_TOKEN.search(line, position)
        if match is None or match.group("comment"):
            break
        position = match.end()
        if match.group(3):
            delimiters.append((match.group(3), bool(match.group(1))))
        else:
            _shell_code_frame(match.group(), stack)
    return delimiters, tuple(stack)


def _shell_input(content: str) -> str:
    pending: deque[tuple[str, bool]] = deque()
    lines: list[str] = []
    frames: tuple[str, ...] = ()
    for line in content.splitlines(keepends=True):
        if pending:
            delimiter, strip_tabs = pending[0]
            end = line.rstrip("\r\n")
            if strip_tabs:
                end = end.lstrip("\t")
            if end == delimiter:
                pending.popleft()
            lines.append(line)
        else:
            delimiters, frames = _heredocs(line, frames)
            pending.extend(delimiters)
            lines.append(PLACEHOLDER.sub(r"\1", line))
    return "".join(lines)


def _check_bash(content: str) -> str | None:
    try:
        done = subprocess.run(
            ["bash", "-n"],
            input=_shell_input(content),
            capture_output=True,
            text=True,
            check=False,
            timeout=DEADLINE_SECONDS,
            env=_shell_env(),
        )
    except FileNotFoundError:
        return "bash not found"
    except subprocess.TimeoutExpired:
        return f"timed out after {DEADLINE_SECONDS} s"
    return _shell_error(done)


def _check_sh(content: str) -> str | None:
    try:
        done = subprocess.run(
            ["sh", "-n"],
            input=_shell_input(content),
            capture_output=True,
            text=True,
            check=False,
            timeout=DEADLINE_SECONDS,
            env=_shell_env(),
        )
    except FileNotFoundError:
        return "sh not found"
    except subprocess.TimeoutExpired:
        return f"timed out after {DEADLINE_SECONDS} s"
    return _shell_error(done)


CHECKERS = {
    "yaml": _check_yaml,
    "yml": _check_yaml,
    "toml": _check_toml,
    "json": _check_json,
    "python": _check_python,
    "py": _check_python,
    "bash": _check_bash,
    "sh": _check_sh,
    "shell": _check_sh,
}


def check_examples(root: Path, paths: list[Path]) -> tuple[list[str], int]:
    problems: list[str] = []
    count = 0
    for path in paths:
        try:
            rel_md = path.relative_to(root).as_posix()
        except ValueError:
            rel_md = path.as_posix()
        text = path.read_text(encoding="utf-8")
        for start_line, lang, content in fenced_blocks(text):
            checker = CHECKERS.get(lang)
            if checker is None:
                continue
            count += 1
            err = (
                f"longer than {MAX_EXAMPLE_CHARS} characters"
                if len(content) > MAX_EXAMPLE_CHARS
                else checker(content)
            )
            if err is not None:
                problems.append(f"{rel_md}:{start_line}: {lang}: {err}")
    return problems, count


def tracked_markdown(root: Path) -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=DEADLINE_SECONDS,
    )
    if proc.returncode != 0:
        raise OSError(f"git ls-files failed: {proc.stderr.strip()}")
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", nargs="?", default="check")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    target_paths: list[Path] = []
    if args.command != "check":
        target_paths.append(Path(args.command))
    if args.paths:
        target_paths.extend(args.paths)
    try:
        if target_paths:
            paths = [(p if p.is_absolute() else args.root / p) for p in target_paths]
        else:
            paths = tracked_markdown(args.root)
        problems, count = check_examples(args.root, paths)
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as err:
        complain(f"ERROR: {err}")
        return 3
    for line in problems:
        complain(f"FAIL: {line}")
    if problems:
        return 1
    say(f"PASS: {len(paths)} Markdown files, {count} examples verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
