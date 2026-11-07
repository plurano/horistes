#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check fenced YAML and TOML code examples in tracked Markdown files.

    examples.py [check] [--root DIR]

Exit codes: 0 pass, 1 parse failure, 3 file or git error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

try:
    from yaml import YAMLError, safe_load_all
except ImportError:
    safe_load_all = None
    YAMLError = Exception

FENCE_START = re.compile(r"^(\s{0,3})(`{3,}|~{3,})(.*)")
DEADLINE_SECONDS = 30


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
            if lang in ("yaml", "yml"):
                if safe_load_all is None:
                    problems.append(f"{rel_md}:{start_line}: {lang}: PyYAML not installed")
                    continue
                count += 1
                try:
                    list(safe_load_all(content))
                except (YAMLError, ValueError) as err:
                    problems.append(f"{rel_md}:{start_line}: {lang}: {err}")
            elif lang == "toml":
                count += 1
                try:
                    tomllib.loads(content)
                except (tomllib.TOMLDecodeError, ValueError) as err:
                    problems.append(f"{rel_md}:{start_line}: {lang}: {err}")
    return problems, count


def tracked_markdown(root: Path) -> list[Path]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "*.md"],
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
