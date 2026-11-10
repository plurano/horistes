#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check relative links and heading anchors in tracked Markdown files.

Inline code spans are not read as links; CommonMark escapes apply and destinations stay raw.

    links.py [check] [--root DIR]

Exit codes: 0 pass, 1 broken link or anchor, 3 file or git error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.parse
from bisect import bisect_left
from pathlib import Path
from string import punctuation

INLINE_LINK = re.compile(r"\[[^\]]*\]\((?:<([^<>\n]+)>|([^)\s]+))(?:\s+[^)]*)?\)")
LINK_DESTINATION = re.compile(r"\((?:<([^<>\n]+)>|([^)\s]+))(?:\s+[^)]*)?\)")
INLINE_MARKER = re.compile(r"`+|\\|\[|\]")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))")
FENCE_START = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?$")
DEADLINE_SECONDS = 30


def say(line: str) -> None:
    """Write one status line to standard output."""
    sys.stdout.write(line + "\n")


def complain(line: str) -> None:
    """Write one diagnostic line to standard error."""
    sys.stderr.write(line + "\n")


def is_skipped(target: str) -> bool:
    """Identify external URLs and same-document anchors excluded from file checks."""
    return target.lower().startswith(("http://", "https://", "mailto:", "#"))


def github_slug(heading: str) -> str:
    """Convert a Markdown heading into its GitHub-style anchor slug."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
    cleaned = re.sub(r"[^\w\s-]", "", re.sub(r"<[^>]+>", "", text).lower()).strip()
    return re.sub(r"\s+", "-", cleaned)


def unfenced_lines(text: str) -> list[str]:
    """Return lines outside matching backtick or tilde fences."""
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
    """Collect heading anchors with suffixes for duplicate slugs."""
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


def _backtick_run_starts(markers: list[re.Match[str]]) -> dict[int, list[int]]:
    """Index maximal raw backtick runs by length in ascending source order."""
    starts: dict[int, list[int]] = {}
    for marker in markers:
        if marker.group()[0] == "`":
            starts.setdefault(marker.end() - marker.start(), []).append(marker.start())
    return starts


def _code_span_end(starts: dict[int, list[int]], start: int, length: int) -> int | None:
    """Locate the first raw closer of the opener's length using binary search."""
    candidates = starts.get(length, [])
    index = bisect_left(candidates, start + length)
    return candidates[index] + length if index < len(candidates) else None


def _escaped_pair_end(line: str, start: int) -> int:
    """Consume ASCII punctuation escaped by a backslash outside a code span."""
    if start + 1 < len(line) and line[start + 1] in punctuation:
        return start + 2
    return start + 1


def _destination_end(line: str, start: int, last_parenthesis: int) -> int:
    """Preserve a link destination only when its inline grammar matches after the label."""
    if start >= last_parenthesis:
        return start + 1
    destination = LINK_DESTINATION.match(line, start + 1)
    return destination.end() if destination else start + 1


def _mask_code_line(line: str) -> str:
    """Mask spans on one physical line while preserving escapes and link destinations."""
    markers = list(INLINE_MARKER.finditer(line))
    starts = _backtick_run_starts(markers)
    masked = list(line)
    resume, brackets = 0, 0
    last_parenthesis = line.rfind(")")
    for marker in markers:
        start = max(marker.start(), resume)
        if start >= marker.end():
            continue
        char = line[start]
        if char == "\\":
            resume = _escaped_pair_end(line, start)
        elif char == "`":
            end = _code_span_end(starts, start, marker.end() - start)
            if end is not None:
                masked[start:end] = " " * (end - start)
                resume = end
        elif char == "[":
            brackets += 1
        elif brackets:
            brackets -= 1
            resume = _destination_end(line, start, last_parenthesis)
    return "".join(masked)


def without_code_spans(line: str) -> str:
    """Mask spans; see test_boundary_without_code_spans_preserves_spacing in tests/test_links.py."""
    return "\n".join(_mask_code_line(part) for part in line.split("\n"))


def extract_links(text: str) -> list[str]:
    """Read inline links outside code spans and reference definitions from raw lines."""
    targets = []
    for line in unfenced_lines(text):
        for m in INLINE_LINK.finditer(without_code_spans(line)):
            t = (m.group(1) or m.group(2)).strip("<>'\"")
            if t and not is_skipped(t):
                targets.append(t)
        m_ref = REF_DEF.match(line)
        if m_ref:
            t = (m_ref.group(1) or m_ref.group(2)).strip("<>'\"")
            if t and not is_skipped(t):
                targets.append(t)
    return targets


def _target_problem(
    root: Path, path: Path, target: str, heading_cache: dict[Path, set[str]]
) -> str | None:
    """Return a missing-file or missing-anchor diagnostic for one relative target."""
    rel_md = path.relative_to(root).as_posix()
    file_part, _, anchor = target.partition("#")
    file_part = urllib.parse.unquote(file_part)
    if not file_part:
        return None
    base = root if file_part.startswith("/") else path.parent
    target_path = base / file_part.lstrip("/")
    if not target_path.exists():
        return f"{rel_md}: target `{file_part}` does not exist"
    is_md = target_path.suffix.lower() in (".md", ".markdown")
    if anchor and target_path.is_file() and is_md:
        if target_path not in heading_cache:
            heading_cache[target_path] = heading_slugs(target_path.read_text(encoding="utf-8"))
        if urllib.parse.unquote(anchor).lower() not in heading_cache[target_path]:
            return f"{rel_md}: anchor `#{anchor}` not found in `{file_part}`"
    return None


def check_links(root: Path, paths: list[Path]) -> tuple[list[str], int]:
    """Check relative targets and Markdown anchors, returning diagnostics and link count."""
    problems, heading_cache, link_count, seen = [], {}, 0, set()
    for path in paths:
        rel_md = path.relative_to(root).as_posix()
        for target in extract_links(path.read_text(encoding="utf-8")):
            link_count += 1
            if (rel_md, target) in seen:
                continue
            seen.add((rel_md, target))
            problem = _target_problem(root, path, target, heading_cache)
            if problem:
                problems.append(problem)
    return problems, link_count


def tracked_markdown(root: Path) -> list[Path]:
    """Ask Git for tracked Markdown paths within the configured deadline."""
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
    """Check tracked Markdown files and return the documented command exit status."""
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
