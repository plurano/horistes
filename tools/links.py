#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check relative links and heading anchors in tracked Markdown files.

    links.py [check] [--root DIR]

Exit codes: 0 pass, 1 broken link or anchor, 3 file or git error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

INLINE_LINK = re.compile(r"\[[^\]]*\]\((?:<([^>]+)>|([^)\s]+))(?:\s+[^)]*)?\)")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))")
FENCE_START = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?$")
DEADLINE_SECONDS = 30


def say(line: str) -> None:
    sys.stdout.write(line + "\n")


def complain(line: str) -> None:
    sys.stderr.write(line + "\n")


def is_skipped(target: str) -> bool:
    return target.lower().startswith(("http://", "https://", "mailto:", "#"))


def github_slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
    cleaned = re.sub(r"[^\w\s-]", "", re.sub(r"<[^>]+>", "", text).lower()).strip()
    return re.sub(r"\s+", "-", cleaned)


def unfenced_lines(text: str) -> list[str]:
    lines, in_fence, fence_char, fence_len = [], False, "", 0
    for line in text.splitlines():
        if not in_fence:
            m = FENCE_START.match(line)
            if m:
                in_fence, fence_char, fence_len = True, m.group(1)[0], len(m.group(1))
                continue
            lines.append(line)
        elif re.match(rf"^\s{{0,3}}{re.escape(fence_char)}{{{fence_len},}}\s*$", line):
            in_fence = False
    return lines


def heading_slugs(text: str) -> set[str]:
    slugs, counts = set(), {}
    for line in unfenced_lines(text):
        m = HEADING.match(line)
        if m:
            slug = github_slug(m.group(1).rstrip("#").strip())
            if slug:
                count = counts.get(slug, 0)
                counts[slug] = count + 1
                slugs.add(slug if count == 0 else f"{slug}-{count}")
    return slugs


def extract_links(text: str) -> list[str]:
    targets = []
    for line in unfenced_lines(text):
        for m in INLINE_LINK.finditer(line):
            t = (m.group(1) or m.group(2)).strip("<>'\"")
            if t and not is_skipped(t):
                targets.append(t)
        m_ref = REF_DEF.match(line)
        if m_ref:
            t = (m_ref.group(1) or m_ref.group(2)).strip("<>'\"")
            if t and not is_skipped(t):
                targets.append(t)
    return targets


def check_links(root: Path, paths: list[Path]) -> tuple[list[str], int]:
    problems, heading_cache, link_count, seen = [], {}, 0, set()
    for path in paths:
        rel_md = path.relative_to(root).as_posix()
        for target in extract_links(path.read_text(encoding="utf-8")):
            link_count += 1
            if (rel_md, target) in seen:
                continue
            seen.add((rel_md, target))
            file_part, _, anchor = target.partition("#")
            file_part = urllib.parse.unquote(file_part)
            if not file_part:
                continue
            base = root if file_part.startswith("/") else path.parent
            target_path = base / file_part.lstrip("/")
            if not target_path.exists():
                problems.append(f"{rel_md}: target `{file_part}` does not exist")
                continue
            is_md = target_path.suffix.lower() in (".md", ".markdown")
            if anchor and target_path.is_file() and is_md:
                if target_path not in heading_cache:
                    heading_cache[target_path] = heading_slugs(
                        target_path.read_text(encoding="utf-8")
                    )
                if urllib.parse.unquote(anchor).lower() not in heading_cache[target_path]:
                    problems.append(f"{rel_md}: anchor `#{anchor}` not found in `{file_part}`")
    return problems, link_count


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
    parser.add_argument("command", nargs="?", default="check", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        paths = tracked_markdown(args.root)
        problems, count = check_links(args.root, paths)
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as err:
        complain(f"ERROR: {err}")
        return 3
    for line in problems:
        complain(f"FAIL: {line}")
    if problems:
        return 1
    say(f"PASS: {len(paths)} Markdown files, {count} relative links verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
