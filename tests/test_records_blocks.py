# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Decision-record blocks behavior and planted regressions."""

from __future__ import annotations

import pytest
import records
from records_cases import (
    APP,
    NEW,
    NEW_RECORD,
    NO_POINTER,
    POINTED,
    UNPOINTED,
    base_repo,
    change,
    failures,
)


def test_a_comment_block_is_no_claim(tree):
    repo = base_repo(tree)
    comment = f"{NEW_RECORD}\n<!-- Reviewed 3 times. -->\n"
    assert change(repo, {NEW: comment}).returncode == 0


@pytest.mark.parametrize(
    ("text", "spans"),
    [
        pytest.param("`a`", ["a"], id="plain"),
        pytest.param("`` a`b ``", ["a`b"], id="double-backticks-strip-one-space"),
        pytest.param("`  `", ["  "], id="blank-span-keeps-its-spaces"),
        pytest.param("`one\ntwo`", ["one two"], id="newline-is-a-space"),
        pytest.param("``a`", [], id="unmatched-run"),
    ],
)
def test_boundary_code_spans(text, spans):
    assert records.code_spans(text) == spans


def test_boundary_blocks_follow_fences_and_headings():
    text = "## Decision\n\n```\n## Context\n```\n####### seven\n- one\n  nested\n- two\n"
    found = records.blocks(text)
    assert [(b.section, b.start, b.end, b.fenced) for b in found] == [
        ("Decision", 3, 5, True),
        ("Decision", 6, 6, False),
        ("Decision", 7, 8, False),
        ("Decision", 9, 9, False),
    ]


@pytest.mark.parametrize(
    "comment",
    [
        """<!--

## Decision

```python
for attempt in range(RETRY_LIMIT):
```

Hidden 42 claims.
-->""",
        '<!-- `DEADLINE_SECONDS = timeout_for("forge")` -->',
    ],
    ids=["hidden-blocks", "hidden-span"],
)
def test_planted_code_and_claims_inside_html_comments_are_ignored(tree, comment):
    repo = base_repo(tree)
    done = change(repo, {NEW: NEW_RECORD + "\n" + comment + "\n"})
    assert done.returncode == 0, done.stderr


def test_planted_comment_cannot_hide_a_visible_claim(tree):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(POINTED, "<!-- explanation --> " + UNPOINTED)
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize(
    "text",
    [
        "`<!-- literal -->`",
        "`` <!-- literal ` marker --> ``",
        "```python\n<!-- literal -->\n```",
    ],
    ids=["span", "doubled-span", "fence"],
)
def test_comment_markers_inside_code_stay_literal(text):
    assert records.visible_text(text) == text


@pytest.mark.parametrize(
    "nested", ["  See `tools/app.py`.", "  - See `tools/app.py`.", "\tSee `tools/app.py`."]
)
def test_planted_list_item_holds_nested_paragraph(tree, nested):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(POINTED, "- The app waits.\n\n" + nested)
    done = change(repo, {NEW: text})
    assert done.returncode == 0, done.stdout + done.stderr
    item = next(block for block in records.blocks(text) if block.start == 12)
    assert item.lines == ["- The app waits.", "", nested]
    assert item.end == 14


def test_list_item_blank_before_unindented_claim_starts_new_block(tree):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(POINTED, "- See `tools/app.py`.\n\nThe app waits.")
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {NEW}:14: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize("next_line", ["plain", "- next", "## Context", "  ## Context", "  ```"])
def test_list_item_does_not_hold_trailing_blanks(next_line):
    found = records.blocks("## Decision\n\n- item\n\n\n" + next_line + "\n")
    assert found[0].lines == ["- item"]
    assert found[0].end == 3
    assert found[1].start == 6 if len(found) > 1 else next_line.lstrip().startswith("##")


def test_paragraph_blank_still_starts_new_block():
    found = records.blocks("## Decision\n\nplain\n\n  nested\n\n")
    assert [(block.start, block.end) for block in found] == [(3, 3), (5, 5)]


@pytest.mark.parametrize(
    ("marker", "indent", "passes"),
    [
        ("- ", " ", False),
        ("- ", "  ", True),
        ("1.  ", "   ", False),
        ("1.  ", "    ", True),
        ("-\t", "   ", False),
        ("-\t", "\t", True),
    ],
)
def test_planted_item_continuation_requires_content_indent(tree, marker, indent, passes):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(
        POINTED, marker + "The app waits.\n\n" + indent + "See `tools/app.py`."
    )
    done = change(repo, {NEW: text})
    assert done.returncode == (0 if passes else 1), done.stdout + done.stderr
    assert failures(done) == (
        [] if passes else [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]
    )


@pytest.mark.parametrize(
    ("line", "indent"),
    [("- item", 2), ("1.  item", 4), ("-     item", 2), ("1.", 3), ("-\titem", 4)],
)
def test_item_content_indent_is_stored_once(line, indent):
    assert records.blocks(line)[0].content_indent == indent


@pytest.mark.parametrize("container", ["-   ", "- \n  - "])
@pytest.mark.parametrize("copied", [True, False], ids=["source", "original"])
def test_planted_nested_fence_judges_source_line(tree, container, copied):
    repo = base_repo(tree)
    body = 'DEADLINE_SECONDS = timeout_for("forge")' if copied else "some original explanation"
    text = NEW_RECORD.replace(
        POINTED,
        container + "The app waits (`tools/app.py`).\n\n    ```python\n    " + body + "\n    ```",
    )
    done = change(repo, {NEW: text})
    assert done.returncode == (1 if copied else 0), done.stdout + done.stderr
    if copied:
        start = 15 if "\n" in container else 14
        assert any(f"{NEW}:{start}: restated code" in line for line in failures(done))
    fenced = [block for block in records.blocks(text) if block.fenced]
    assert len(fenced) == 1
    assert fenced[0].lines[1].strip() == body


@pytest.mark.parametrize("marker", ["```", "~~~"])
def test_nested_fence_comment_markers_stay_literal(marker):
    text = "- item\n\n    " + marker + "python\n    <!-- literal ` marker\n    " + marker + "\n"
    assert records.visible_text(text) == text
    assert records.blocks(text)[1].fenced


def test_unclosed_nested_fence_ends_with_its_item(tree):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(
        POINTED,
        "- The app waits (`tools/app.py`).\n\n    ```python\n"
        "    original explanation <!-- literal marker\n\nThe app waits.",
    )
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {NEW}:17: claim under 'Decision' {NO_POINTER}"]
    assert not records.blocks(text)[-2].fenced


@pytest.mark.parametrize("container", ["- ", "1. "])
@pytest.mark.parametrize("marker", ["~~~", "```"])
@pytest.mark.parametrize("copied", [True, False], ids=["source", "original"])
def test_planted_marker_line_fence_judges_source_line(tree, container, marker, copied):
    repo = base_repo(tree)
    body = 'DEADLINE_SECONDS = timeout_for("forge")' if copied else "some original explanation"
    assert not copied or (body in APP and len(body) >= records.MIN_RESTATED_CHARS)
    indent = " " * len(container)
    text = NEW_RECORD.replace(
        POINTED,
        "- The app waits (`tools/app.py`).\n\n"
        f"{container}{marker}python\n{indent}{body}\n{indent}{marker}",
    )
    done = change(repo, {NEW: text})
    assert done.returncode == (1 if copied else 0), done.stdout + done.stderr
    if copied:
        assert any(f"{NEW}:14: restated code" in line for line in failures(done))


@pytest.mark.parametrize("container", ["- ", "1. ", "-    ", "-\t"])
@pytest.mark.parametrize("marker", ["~~~", "```"])
def test_marker_line_fence_closes_before_next_paragraph(container, marker):
    indent = " " * len(container.expandtabs(4))
    text = (
        f"## Decision\n\n{container}{marker}python\n{indent}body\n{indent}{marker}\n"
        "\nNext paragraph.\n"
    )
    found = records.blocks(text)
    assert [(b.start, b.end, b.fenced) for b in found] == [(3, 5, True), (7, 7, False)]
    assert found[-1].lines == ["Next paragraph."]


@pytest.mark.parametrize("container", ["- ", "1. "])
@pytest.mark.parametrize("marker", ["~~~", "```"])
def test_marker_line_fence_comment_markers_stay_literal(container, marker):
    indent = " " * len(container)
    text = f"{container}{marker}python\n{indent}<!-- literal ` marker\n{indent}{marker}\n"
    assert records.visible_text(text) == text


@pytest.mark.parametrize("line", ["-", "-     ~~~", "1.     ```"])
def test_empty_or_excess_padding_item_opens_no_fence(line):
    found = records.blocks(line)
    assert len(found) == 1
    assert not found[0].fenced
    assert found[0].content_indent == (3 if line.startswith("1.") else 2)


@pytest.mark.parametrize(
    "separator", ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_non_lf_separators_keep_git_block_lines_and_comment_positions(separator):
    text = f"# Title{separator}subtitle\n\n## Decision\n\nClaim{separator}continued.\n"
    found = records.blocks(text)
    assert [(b.start, b.end, b.lines) for b in found] == [(5, 5, [f"Claim{separator}continued."])]
    comment = f"<!-- hidden{separator}text -->"
    text += f"\n{comment}\nVisible.\n"
    masked = records.visible_text(text)
    assert masked == text.replace(comment, " " * len(comment))
    assert len(masked) == len(text)
    assert [i for i, c in enumerate(masked) if c == "\n"] == [
        i for i, c in enumerate(text) if c == "\n"
    ]


def test_crlf_record_matches_lf_blocks_and_preserves_comment_positions():
    text = "## Decision\n\n- ~~~python\n  <!-- literal\n  ~~~\n\n<!-- hidden -->\nClaim.\n"
    crlf = text.replace("\n", "\r\n")
    assert records.blocks(crlf) == records.blocks(text)
    assert records.visible_text(crlf) == crlf.replace("<!-- hidden -->", " " * 15)


def test_blocks_strip_only_the_cr_touching_lf():
    found = records.blocks("## Decision\n\nClaim\r\r\ncontinued\r")
    assert [(b.start, b.end, b.lines) for b in found] == [(3, 4, ["Claim\r", "continued\r"])]


@pytest.mark.parametrize("later_fence", [False, True], ids=["span-only", "later-fence"])
def test_planted_backticks_in_fence_info_leave_a_judged_span(later_fence):
    source = "CLOCK_SKEW_MINUTES = 5"
    tree = records.Tree(frozenset(), source, frozenset())
    text = f"# Record\n\n## Decision\n\n```{source}```\n"
    if later_fence:
        text += "\n```python\nx = 1\n```\n\nAfter the fence.\n"
    found = records.blocks(text)
    assert records.restated(found[0], tree) == [source]
    assert records.judge(NEW, found[0], tree) == [
        f"{NEW}:5: restated code, cite the file instead: `{source}`",
        f"{NEW}:5: claim under 'Decision' {NO_POINTER}",
    ]
    if later_fence:
        assert [(b.start, b.end, b.fenced) for b in found] == [
            (5, 5, False),
            (7, 9, True),
            (11, 11, False),
        ]


@pytest.mark.parametrize("opener", ["```python", "~~~ a`b`"], ids=["backtick", "tilde-info"])
def test_valid_fence_info_preserves_body_and_literal_comments(opener):
    closer = opener[:3]
    text = f"## Decision\n\n{opener}\nx = 1\n<!-- literal -->\n{closer}\n"
    found = records.blocks(text)
    assert records.visible_text(text) == text
    assert [(b.start, b.end, b.fenced) for b in found] == [(3, 6, True)]
    assert records.judge(NEW, found[0], records.Tree(frozenset(), "", frozenset())) == []


@pytest.mark.parametrize(
    ("first", "second", "content_indent"),
    [("- ", "- ", 2), (" - ", " - ", 3), ("   1. ", "   2. ", 6)],
    ids=["column-zero", "one-space-bullet", "three-space-ordered"],
)
def test_planted_indented_top_items_cannot_share_a_pointer(first, second, content_indent):
    text = f"# Record\n\n## Decision\n\n{first}The app waits.\n{second}See `tools/records.py`.\n"
    found = records.blocks(text)
    tree = records.Tree(frozenset({"tools/records.py"}), "", frozenset())
    assert [p for b in found for p in records.judge(NEW, b, tree)] == [
        f"{NEW}:5: claim under 'Decision' {NO_POINTER}"
    ]
    assert [(b.start, b.end, b.content_indent) for b in found] == [
        (5, 5, content_indent),
        (6, 6, content_indent),
    ]


@pytest.mark.parametrize(
    ("parent", "nested"),
    [("- ", "  - "), (" - ", "   - "), ("- ", "\t- ")],
    ids=["at-content-column", "indented-parent", "tab-expanded"],
)
def test_planted_nested_item_keeps_its_parents_pointer(parent, nested):
    text = f"## Decision\n\n{parent}Parent see `tools/records.py`.\n{nested}nested claim\n"
    found = records.blocks(text)
    assert [(b.start, b.end) for b in found] == [(3, 4)]
    tree = records.Tree(frozenset({"tools/records.py"}), "", frozenset())
    assert records.judge(NEW, found[0], tree) == []


@pytest.mark.parametrize("indent", ["    ", "\t"], ids=["four-spaces", "tab"])
def test_four_column_marker_continues_the_paragraph(indent):
    found = records.blocks(f"## Decision\n\nParagraph.\n{indent}- x\n")
    assert [(b.start, b.end, b.content_indent) for b in found] == [(3, 4, 0)]


@pytest.mark.parametrize("container", [" - ", "   1. "])
def test_indented_marker_fence_uses_the_items_content_column(container):
    indent = " " * len(container)
    text = f"## Decision\n\n{container}```python\n{indent}<!-- literal -->\n{indent}```\n"
    assert records.visible_text(text) == text
    found = records.blocks(text)
    assert [(b.start, b.end, b.fenced) for b in found] == [(3, 5, True)]
