#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check what a change adds to the decision records: no restated code, every claim points somewhere.

Only blocks holding added lines or surviving text contiguous with removed lines since
the merge base of --base-ref and HEAD are judged (ADR-0089). An accepted record stays
as written (docs/adr/README.md), so the rules bind it again whenever a line of it changes,
and never through an exception list.
A block is a paragraph, a top-level list item with its nested lines, or a fenced block.

- restated code: a code span or fenced line of MIN_RESTATED_CHARS or more that is not a
  bare path, identifier or command, and that a tracked file outside docs/ and Markdown
  holds verbatim;
- claim without a pointer: a block under `## Decision` or `## Consequences`, or a block
  outside `## References` that states a number, with no code span naming a tracked file,
  a command in COMMANDS or a test function, and no link to a tracked file or a web page.

    records.py [check] [--root DIR] [--base-ref REF]

Exit codes: 0 pass, 1 finding, 3 file or git error; a base with no merge base is an error.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import ciwf
from ciwf import NULL_IDS
from links import INLINE_LINK

RECORD = re.compile(r"docs/adr/\d{4}-[^/]+\.md")
DEADLINE_SECONDS = 30
# Records held 88 files and the tree 640 tracked files on 2026-11-16.
MAX_FILES = 20_000
MAX_LINES = 20_000
MAX_SOURCE_BYTES = 1_000_000  # Also bounds records before decoding or counting their lines.
# The shortest span the reviewed record restated (`CLOCK_SKEW_MINUTES = 5`) has 22 characters.
MIN_RESTATED_CHARS = 20
# The command surfaces the agent instructions name as verification commands.
COMMANDS = ("make", "praetorctl")
CLAIM_SECTIONS = ("Decision", "Consequences")
UNCOUNTED_SECTIONS = ("References",)

# Against the work tree, so a local run judges uncommitted records too.
DIFF = (
    "diff",
    "--unified=0",
    "--inter-hunk-context=0",
    "--no-renames",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--text",
    "--default-prefix",
)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)(?:[ \t]+#+)?)?[ \t]*$")
TOP_ITEM = re.compile(r"^(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")
CODE_SPAN = re.compile(r"(?<!`)(?P<ticks>`+)(?!`)(?P<code>.+?)(?<!`)(?P=ticks)(?!`)", re.DOTALL)
MARKDOWN_TOKEN = re.compile(r"`+|<!--")
# A bare path, identifier, flag or call names code; it does not restate it.
POINTER_SHAPE = re.compile(r"[\w.:/@+\[\]-]+(?:\(\))?")
CODE_SHAPE = re.compile(r"[=\{\}\"']|\w\(.+\)|\s")
TEST_NAME = re.compile(r"test_\w+")
TEST_DEFINITION = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)
UNCOUNTED_NUMBERS = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+Z?)?|ADR-\d{4}|^\s*(?:[-*+]\s+)?\d{1,9}[.)]\s", re.MULTILINE
)
DIGIT = re.compile(r"\d")


@dataclass
class Block:
    """Lines of one record that the rules judge together."""

    section: str
    start: int
    lines: list[str] = field(default_factory=list)
    fenced: bool = False
    content_indent: int = 0

    @property
    def end(self) -> int:
        return self.start + len(self.lines) - 1

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Tree:
    """What the rules read besides the record: tracked paths, source text and test names."""

    paths: frozenset[str]
    source: str
    tests: frozenset[str]


def git(root: Path, *args: str) -> str:
    """Run git in root under the deadline; a failure is a ReadError naming the command."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=DEADLINE_SECONDS,
            env=ciwf.git_env(),
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as err:
        raise ciwf.ReadError(f"git {args[0]}: {err}") from err
    if done.returncode != 0:
        raise ciwf.ReadError(f"git {' '.join(args)}: {done.stderr.strip() or done.returncode}")
    return done.stdout


def merge_base(root: Path, ref: str) -> str:
    """The commit HEAD and ref share; without one the check cannot tell what changed."""
    if ref in NULL_IDS:
        raise ciwf.ReadError(f"base {ref} is the null commit: no merge base to compare with")
    try:
        return git(root, "merge-base", ref, "HEAD").strip()
    except ciwf.ReadError as err:
        raise ciwf.ReadError(f"no merge base of {ref} and HEAD: {err}") from err


@dataclass
class Deletion:
    """New-side boundary and whether the removed run began and ended with text."""

    point: int
    first: bool
    last: bool


@dataclass
class Changes:
    """Added lines and contiguous removed runs from one bounded diff walk."""

    added: set[int] = field(default_factory=set)
    deletions: list[Deletion] = field(default_factory=list)


def git_lines(text: str, *, keepends: bool = False) -> list[str]:
    """Split only at LF, stripping CR only before LF unless endings must stay exact."""
    parts = text.split("\n")
    if keepends:
        lines = [line + "\n" for line in parts[:-1]]
    else:
        lines = [line.removesuffix("\r") for line in parts[:-1]]
    return lines + ([parts[-1]] if parts[-1] else [])


def diff_changes(diff: str) -> Changes:
    """Walk new-side coordinates once, including zero-length hunks and removed runs."""
    changes = Changes()
    number, deleting = None, False
    for line in limited(git_lines(diff), MAX_LINES * 10, "diff line limit"):
        hunk = HUNK.match(line)
        if hunk:
            number = int(hunk.group(1)) + (hunk.group(2) == "0")
        elif number is None or line.startswith("\\"):
            continue
        elif line.startswith("-"):
            nonblank = bool(line[1:].strip())
            if deleting:
                changes.deletions[-1].last = nonblank
            else:
                changes.deletions.append(Deletion(number - 1, nonblank, nonblank))
            deleting = True
            continue
        elif line.startswith("+"):
            changes.added.add(number)
            number += 1
        elif line.startswith(" "):
            number += 1
        deleting = False
    return changes


def added_lines(diff: str) -> set[int]:
    """Added line numbers from the shared diff walk, independent of path headers."""
    return diff_changes(diff).added


def affected_lines(text: str, changes: Changes) -> set[int]:
    """Bind only nonblank surviving neighbours contiguous with a removed run's text."""
    lines = limited(git_lines(text), MAX_LINES, "record line limit")
    affected = changes.added.copy()
    for deletion in changes.deletions[: MAX_LINES * 10]:
        point = deletion.point
        if deletion.first and 0 < point <= len(lines) and lines[point - 1].strip():
            affected.add(point)
        if deletion.last and point < len(lines) and lines[point].strip():
            affected.add(point + 1)
    return affected


def read_tree(root: Path) -> Tree:
    """Tracked paths, the text of tracked source outside docs/ and Markdown, and test names."""
    paths = limited(
        [p for p in git(root, "ls-files", "-z").split("\0") if p], MAX_FILES, "tracked file limit"
    )
    texts, tests = [], set()
    for name in paths:
        if name.startswith("docs/") or name.endswith(".md"):
            continue
        text = _source_text(root / name)
        texts.append(text)
        if name.startswith("tests/") and name.endswith(".py"):
            tests.update(TEST_DEFINITION.findall(text))
    return Tree(frozenset(paths), "\n".join(texts), frozenset(tests))


def _source_text(path: Path) -> str:
    """Regular tracked text only; missing, binary and non-regular files hold no source."""
    return _bounded_text(path, source=True)


def _bounded_text(path: Path, *, source: bool = False) -> str:
    """Read regular UTF-8 files under a byte limit; only sources may hold no text."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            if source:
                return ""
            raise ciwf.ReadError(f"{path}: record is not a regular file")
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except FileNotFoundError as err:
        if source:
            return ""
        raise ciwf.ReadError(f"{path}: {err}") from err
    except OSError as err:
        raise ciwf.ReadError(f"{path}: {err}") from err
    kind = "source" if source else "record"
    if len(data) > MAX_SOURCE_BYTES:
        raise ciwf.ReadError(f"{path}: {kind} byte limit {MAX_SOURCE_BYTES} exceeded")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as err:
        if source:
            return ""
        raise ciwf.ReadError(f"{path}: {err}") from err


def limited(items: list[str], bound: int, label: str) -> list[str]:
    """Refuse a partial judgment instead of silently truncating the input."""
    if len(items) > bound:
        raise ciwf.ReadError(f"{label} {bound} exceeded")
    return items


def record_text(path: Path) -> str:
    """Read one regular record under byte and line limits, with contextual input errors."""
    text = _bounded_text(path)
    limited(git_lines(text), MAX_LINES, "record line limit")
    return text


def _indent(line: str) -> int:
    """Leading columns with CommonMark's four-column tab stops."""
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def _item_indent(line: str) -> int:
    """Content column: marker width plus one for an empty item or excess whitespace."""
    expanded = line.expandtabs(4)
    item = TOP_ITEM.match(expanded)
    if item is None:
        return 0
    marker = len(expanded.split(maxsplit=1)[0])
    tail = expanded[marker:]
    padding = len(tail) - len(tail.lstrip(" "))
    return marker + (padding if tail.strip() and 1 <= padding <= 4 else 1)


def _breaks_block(line: str, current: Block | None, number: int) -> bool:
    """Headings and items start blocks; after blanks only indented item lines continue."""
    if HEADING.match(line) or TOP_ITEM.match(line):
        return True
    if current is None or number == current.end + 1:
        return False
    return not (current.content_indent and _indent(line) >= current.content_indent)


def _closing_fence(fence: str) -> re.Pattern[str]:
    """The opening character, at least as many, then only blanks, at a line boundary."""
    return re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", re.MULTILINE)


def _closes(line: str, fence: str) -> bool:
    """A closing fence: the opening character, at least as many, then only blanks."""
    return bool(_closing_fence(fence).fullmatch(line))


@dataclass
class FenceState:
    """Container and active fence columns shared by masking and block parsing."""

    marker: str = ""
    indent: int = 0
    item_indent: int = 0


def _find_fence(line: str, content_indent: int) -> tuple[re.Match[str] | None, int]:
    """Apply the unchanged fence rule after stripping a containing item's columns."""
    offset = content_indent if TOP_ITEM.match(line) or _indent(line) >= content_indent else 0
    return FENCE.match(line.expandtabs(4)[offset:]), offset


def _fence_step(line: str, state: FenceState) -> str:
    """Open, continue or close a fence; a nested fence also closes with its container."""
    if state.marker and state.indent and line.strip() and _indent(line) < state.indent:
        state.marker = ""
    if state.marker:
        if _closes(line.expandtabs(4)[state.indent :], state.marker):
            state.marker = ""
            return "close"
        return "body"
    if TOP_ITEM.match(line):
        state.item_indent = _item_indent(line)
    elif line.strip() and _indent(line) < state.item_indent:
        state.item_indent = 0
    opened, indent = _find_fence(line, state.item_indent)
    if opened:
        state.marker, state.indent = opened.group(1), indent
        return "open"
    return ""


def _section(line: str, section: str) -> str:
    """The `## ` heading a line opens, else the section it stays in."""
    heading = HEADING.match(line)
    if heading and len(heading.group(1)) == 2:
        return (heading.group(2) or "").strip()
    return section


def _mask_tokens(text: str, masked: list[str], position: int, end: int) -> int:
    """Mask comments starting on this line; spans and comments may finish on later lines."""
    for _ in range(end - position + 1):
        token = MARKDOWN_TOKEN.search(text, position, end)
        if token is None:
            break
        position = token.end()
        if token.group() == "<!--":
            closing = text.find("-->", position)
            position = closing + 3 if closing >= 0 else len(text)
            start = token.start()
            masked[start:position] = ["\n" if c == "\n" else " " for c in text[start:position]]
        else:
            span = CODE_SPAN.match(text, token.start())
            if span is not None:
                position = span.end()
    return position


def visible_text(text: str) -> str:
    """Mask HTML comments, preserving positions and literal markers in spans and fences."""
    masked, offset, literal_until = list(text), 0, 0
    state = FenceState()
    for line in limited(git_lines(text, keepends=True), MAX_LINES, "record line limit"):
        end = offset + len(line)
        if offset >= literal_until and _fence_step(
            line.removesuffix("\r\n").removesuffix("\n"), state
        ):
            literal_until = end
        else:
            literal_until = _mask_tokens(text, masked, max(offset, literal_until), end)
        offset = end
    return "".join(masked)


def _plain_line(
    found: list[Block], current: Block | None, section: str, number: int, lines: list[str]
) -> Block | None:
    """Collect one non-fenced line, preserving blanks only inside a continued item."""
    line = lines[number - 1]
    if HEADING.match(line):
        return None
    if not line.strip():
        return current
    if _breaks_block(line, current, number):
        current = None
    if current is None or current.fenced:
        current = Block(section, number, content_indent=_item_indent(line))
        found.append(current)
    current.lines.extend(lines[current.end : number])
    return current


def blocks(text: str) -> list[Block]:
    """Split a record into blocks, each with its `## ` section and first line number."""
    found: list[Block] = []
    section, current = "", None
    state = FenceState()
    lines = limited(git_lines(visible_text(text)), MAX_LINES, "record line limit")
    for number, line in enumerate(lines, start=1):
        event = _fence_step(line, state)
        if event == "open":
            current = Block(section, number, [line], fenced=True)
            found.append(current)
            continue
        if event in ("body", "close"):
            current.lines.append(line)
            if event == "close":
                current = None
            continue
        section = _section(line, section)
        current = _plain_line(found, current, section, number, lines)
    return found


def code_spans(text: str) -> list[str]:
    """Code span contents, one surrounding space stripped as CommonMark does, newlines as spaces."""
    spans = []
    for match in CODE_SPAN.finditer(text):
        body = match.group("code").replace("\n", " ")
        if len(body) > 2 and body.startswith(" ") and body.endswith(" ") and body.strip():
            body = body[1:-1]
        spans.append(body)
    return spans


def names_code(span: str, tree: Tree) -> bool:
    """A span that only names code: a bare path, identifier, flag, call or a command."""
    words = span.split()
    if not words or POINTER_SHAPE.fullmatch(span) or not CODE_SHAPE.search(span):
        return True
    return words[0] in COMMANDS or _tracked(words[0], tree)


def _tracked(name: str, tree: Tree) -> bool:
    """A tracked file or directory, with an optional `:line`, `::name` or `#anchor` suffix."""
    path = re.split(r"::|:(?=\d)|#", name, maxsplit=1)[0].rstrip("/")
    if not path:
        return False
    return path in tree.paths or any(p.startswith(path + "/") for p in tree.paths)


def points_somewhere(block: Block, record: str, tree: Tree) -> bool:
    """The block names a tracked file, a command, a test function, or links a file or page."""
    for span in code_spans(block.text):
        head = span.split()[0] if span.split() else ""
        if head in COMMANDS or _tracked(head, tree):
            return True
        test = head.removesuffix("()")
        if TEST_NAME.fullmatch(test) and test in tree.tests:
            return True
    for link in INLINE_LINK.finditer(CODE_SPAN.sub("", block.text)):
        destination = link.group(1) or link.group(2)
        if destination.startswith(("https://", "http://")):
            return True
        target = posixpath.normpath(posixpath.join(posixpath.dirname(record), destination))
        if destination.split("#")[0] and _tracked(target, tree):
            return True
    return False


def _link_label(match: re.Match[str]) -> str:
    """Keep numeric prose in a label while removing its link destination."""
    return match.group().split("](", 1)[0]


def states_number(block: Block) -> bool:
    """A digit outside code spans and links, once dates, record names and list numbers are out."""
    prose = INLINE_LINK.sub(_link_label, CODE_SPAN.sub("", block.text))
    return bool(DIGIT.search(UNCOUNTED_NUMBERS.sub("", prose)))


def restated(block: Block, tree: Tree) -> list[str]:
    """Spans or fenced lines of the block that tracked source holds verbatim."""
    if block.fenced:
        candidates = [line.strip() for line in block.lines[1:]]
    else:
        candidates = code_spans(block.text)
    return [
        text
        for text in candidates
        if len(text) >= MIN_RESTATED_CHARS and not names_code(text, tree) and text in tree.source
    ]


def judge(record: str, block: Block, tree: Tree) -> list[str]:
    """Findings for one changed block of a record."""
    problems = [
        f"{record}:{block.start}: restated code, cite the file instead: `{text[:80]}`"
        for text in restated(block, tree)
    ]
    if block.fenced:
        return problems
    claim = block.section in CLAIM_SECTIONS or (
        block.section not in UNCOUNTED_SECTIONS and states_number(block)
    )
    if claim and not points_somewhere(block, record, tree):
        problems.append(
            f"{record}:{block.start}: claim under '{block.section}' names no tracked file,"
            " command, test function or link"
        )
    return problems


def _record_changes(root: Path, name: str, diff: str) -> Changes | None:
    """Refuse nontext diffs and skip removed records before binding any surviving text."""
    if any(line.startswith(("Binary files ", "GIT binary patch")) for line in git_lines(diff)):
        raise ciwf.ReadError(f"{name}: binary record diff")
    if "\n+++ /dev/null\n" in "\n" + diff or not os.path.lexists(root / name):
        return None
    changes = diff_changes(diff)
    return changes if changes.added or changes.deletions else None


def changed_records(root: Path, base: str) -> dict[str, Changes]:
    """Added lines and deletion boundaries plus local new records absent from git diff."""
    names = git(root, "diff", "-z", "--name-only", "--no-renames", base, "--", "docs/adr")
    changed: dict[str, Changes] = {}
    diff_lines = 0
    for name in limited(names.rstrip("\0").split("\0"), MAX_FILES, "changed file limit"):
        if not RECORD.fullmatch(name):
            continue
        diff = git(root, *DIFF, base, "--", f":(literal){name}")
        diff_lines += len(git_lines(diff))
        if diff_lines > MAX_LINES * 10:
            raise ciwf.ReadError(f"diff line limit {MAX_LINES * 10} exceeded")
        changes = _record_changes(root, name, diff)
        if changes is not None:
            changed[name] = changes
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", "docs/adr")
    for name in limited(untracked.rstrip("\0").split("\0"), MAX_FILES, "new file limit"):
        if RECORD.fullmatch(name):
            count = len(git_lines(record_text(root / name)))
            changed[name] = Changes(set(range(1, count + 1)))
    return changed


def check_records(root: Path, base_ref: str) -> tuple[list[str], int, int]:
    """Findings, changed record count and judged block count against the merge base of base_ref."""
    base = merge_base(root, base_ref)
    changed = changed_records(root, base)
    tree = read_tree(root) if changed else Tree(frozenset(), "", frozenset())
    problems, judged = [], 0
    for record in limited(sorted(changed), MAX_FILES, "changed file limit"):
        text = record_text(root / record)
        lines = affected_lines(text, changed[record])
        for block in blocks(text):
            if any(block.start <= number <= block.end for number in lines):
                judged += 1
                problems.extend(judge(record, block, tree))
    return problems, len(changed), judged


def main(argv: list[str] | None = None) -> int:
    """Check the changed decision records and return the documented exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", nargs="?", default="check", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--base-ref", default="origin/main")
    args = parser.parse_args(argv)
    try:
        problems, changed, judged = check_records(args.root, args.base_ref)
    except ciwf.ReadError as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    for line in problems:
        ciwf.complain(f"FAIL: {line}")
    if problems:
        return 1
    ciwf.say(
        f"PASS: {changed} decision record(s) changed against {args.base_ref},"
        f" {judged} changed block(s) judged"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
