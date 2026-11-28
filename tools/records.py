#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check what a change adds to the decision records: no restated code, every claim points somewhere.

Only blocks holding added lines, neighbours of added blanks, or surviving text contiguous
with removed lines since the merge base of --base-ref and HEAD are judged (ADR-0089).
An accepted record stays as written (docs/adr/README.md). The rules bind it again
whenever a line changes, and never through an exception list.
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
import ast
import posixpath
import re
import sys
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

import ciwf
from links import INLINE_LINK
from records_diff import (
    MAX_FILES,
    MAX_LINES,
    MAX_SOURCE_BYTES,
    Changes,
    _bounded_text,
    changed_records,
    git,
    git_lines,
    limited,
    merge_base,
    record_text,
)

MAX_AST_NODES = MAX_SOURCE_BYTES

# The shortest span the reviewed record restated (`CLOCK_SKEW_MINUTES = 5`) has 22 characters.
MIN_RESTATED_CHARS = 20
# The command surfaces the agent instructions name as verification commands.
COMMANDS = ("make", "praetorctl")
CLAIM_SECTIONS = ("Decision", "Consequences")
UNCOUNTED_SECTIONS = ("References",)

FENCE = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)(?:[ \t]+#+)?)?[ \t]*$")
TOP_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")
CODE_SPAN = re.compile(
    r"(?<!`)(?P<ticks>`+)(?!`)(?P<code>(?:(?!\n[ \t]*\r?\n).)+?)(?<!`)(?P=ticks)(?!`)", re.DOTALL
)
MARKDOWN_TOKEN = re.compile(r"`+|<!--")
# A bare path, identifier, flag or call names code; it does not restate it.
POINTER_SHAPE = re.compile(r"[\w.:/@+\[\]-]+(?:\(\))?")
CODE_SHAPE = re.compile(r"[=\{\}\"']|\w\(.+\)|\s")
TEST_NAME = re.compile(r"test_\w+")
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


def _blank_neighbours(lines: list[str], added: set[int]) -> set[int]:
    """Bind both ends of a blank run containing an added blank, in one bounded pass."""
    neighbours, previous, changed = set(), 0, False
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            changed = changed or number in added
            continue
        if changed:
            neighbours.update((previous, number))
        previous, changed = number, False
    if changed:
        neighbours.add(previous)
    return neighbours - {0}


def affected_lines(text: str, changes: Changes) -> set[int]:
    """Bind removed text's contiguous neighbours and added blanks' nearest nonblank neighbours."""
    lines = limited(git_lines(text), MAX_LINES, "record line limit")
    affected = changes.added | _blank_neighbours(lines, changes.added)
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
            tests.update(_test_definitions(name, text))
    return Tree(frozenset(paths), "\n".join(texts), frozenset(tests))


def _test_definitions(name: str, text: str) -> set[str]:
    """Parse without execution; refuse invalid syntax or an oversized iterative AST walk."""
    try:
        parsed = ast.parse(text, filename=name)
    except (SyntaxError, ValueError, RecursionError) as err:
        raise ciwf.ReadError(f"{name}: cannot parse test source: {err}") from err
    tests = set()
    for count, node in enumerate(islice(ast.walk(parsed), MAX_AST_NODES + 1), start=1):
        if count > MAX_AST_NODES:
            raise ciwf.ReadError(f"{name}: AST node limit {MAX_AST_NODES} exceeded")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            tests.add(node.name)
    return tests


def _source_text(path: Path) -> str:
    """Regular tracked text only; missing, binary and non-regular files hold no source."""
    return _bounded_text(path, source=True)


def _indent(line: str) -> int:
    """Leading columns with CommonMark's four-column tab stops."""
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def _item_indent(line: str) -> int:
    """Content column: indent, marker, padding (one for empty items or excess whitespace)."""
    expanded = line.expandtabs(4)
    item = TOP_ITEM.match(expanded)
    if item is None:
        return 0
    marker = _indent(expanded) + len(expanded.split(maxsplit=1)[0])
    tail = expanded[marker:]
    padding = len(tail) - len(tail.lstrip(" "))
    return marker + (padding if tail.strip() and 1 <= padding <= 4 else 1)


def _breaks_block(line: str, current: Block | None, number: int) -> bool:
    """Headings and items start blocks; after blanks only indented item lines continue."""
    content_indent = current.content_indent if current else 0
    item = _item_indent(line) and (not content_indent or _indent(line) < content_indent)
    if HEADING.match(line) or item:
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
    """Apply the shared fence rule after stripping a containing item's columns."""
    offset = content_indent if _item_indent(line) or _indent(line) >= content_indent else 0
    return FENCE.match(line.expandtabs(4)[offset:]), offset


def _outside_item(line: str, content_indent: int) -> bool:
    """A nonblank line before the item's content column leaves its container."""
    return bool(line.strip()) and _indent(line) < content_indent


def _fence_step(line: str, state: FenceState) -> str:
    """Open, continue or close a fence; a nested fence also closes with its container."""
    if state.marker and _outside_item(line, state.indent):
        state.marker = ""
    if state.marker:
        if _closes(line.expandtabs(4)[state.indent :], state.marker):
            state.marker = ""
            return "close"
        return "body"
    if item_indent := _item_indent(line):
        state.item_indent = item_indent
    elif _outside_item(line, state.item_indent):
        state.item_indent = 0
    opened, indent = _find_fence(line, state.item_indent)
    if opened:
        state.marker, state.indent = opened.group(1), indent
        return "open"
    return ""


def _section(line: str, section: str, content_indent: int) -> str:
    """The `## ` heading a line opens, else the section it stays in."""
    if content_indent and _indent(line) >= content_indent:
        return section
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
        section = _section(line, section, current.content_indent if current else state.item_indent)
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


def _span_pointer(span: str, tree: Tree) -> bool:
    """A span's first word can name a command, tracked path or parsed test function."""
    words = span.split()
    head = words[0] if words else ""
    test = head.removesuffix("()")
    return (
        head in COMMANDS
        or _tracked(head, tree)
        or bool(TEST_NAME.fullmatch(test) and test in tree.tests)
    )


def points_somewhere(block: Block, record: str, tree: Tree) -> bool:
    """The block names a tracked file, a command, a test function, or links a file or page."""
    if any(_span_pointer(span, tree) for span in code_spans(block.text)):
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
