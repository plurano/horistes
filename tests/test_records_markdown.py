# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Markdown block boundaries and parsed test-function evidence."""

from __future__ import annotations

import pytest
import records
from records_cases import NEW, NO_POINTER, OLD, base_repo


@pytest.mark.parametrize(
    ("source", "name", "pointed"),
    [
        ('"""\ndef test_ghost():\n    pass\n"""\n', "test_ghost", False),
        ("# def test_ghost():\n", "test_ghost", False),
        ("def test_real():\n    pass\n", "test_real", True),
        ("async def test_async_real():\n    pass\n", "test_async_real", True),
    ],
    ids=["string-ghost", "comment-ghost", "real", "async-real"],
)
def test_planted_test_pointer_requires_ast_definition(tree, source, name, pointed):
    repo = base_repo(tree)
    repo.write({"tests/test_indexed.py": source})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "tracked test definitions")
    repo.write({NEW: f"## Decision\n\nThe app retries (`{name}()`).\n"})
    problems, changed, judged = records.check_records(repo.root, "main")
    assert (changed, judged) == (1, 1)
    assert problems == ([] if pointed else [f"{NEW}:3: claim under 'Decision' {NO_POINTER}"])


def test_planted_unparseable_test_source_fails_closed(tree):
    repo = base_repo(tree)
    repo.write({"tests/test_broken.py": "def test_broken(:\n"})
    repo.git("add", "-A")
    with pytest.raises(records.ciwf.ReadError, match="tests/test_broken.py.*parse"):
        records.read_tree(repo.root)


def test_planted_test_ast_node_limit_fails_closed(tree, monkeypatch):
    repo = base_repo(tree)
    monkeypatch.setattr(records, "MAX_AST_NODES", 1, raising=False)
    with pytest.raises(records.ciwf.ReadError, match="tests/test_app.py.*AST node limit 1"):
        records.read_tree(repo.root)


@pytest.mark.parametrize("blank", ["", " \t ", "\r"], ids=["empty", "whitespace", "crlf"])
def test_planted_backticks_across_blank_lines_do_not_expose_comments(blank):
    text = f"## Context\n\nParagraph ends `\n{blank}\n<!-- hidden 42 -->\n\nAnother ends `\n"
    masked = records.visible_text(text)
    assert masked == text.replace("<!-- hidden 42 -->", " " * len("<!-- hidden 42 -->"))
    tree = records.Tree(frozenset(), "", frozenset())
    assert [p for block in records.blocks(text) for p in records.judge(NEW, block, tree)] == []
    assert records.code_spans(text) == []


def test_code_span_block_boundary_agrees_with_link_and_number_checks():
    text = "Before `\n\n42 [evidence](https://example.invalid)\n\nAfter `"
    block = records.Block("Context", 1, [text])
    assert records.code_spans(text) == []
    assert records.states_number(block)
    assert records.points_somewhere(block, NEW, records.Tree(frozenset(), "", frozenset()))


@pytest.mark.parametrize(
    "indent", ["", " ", "  ", "   "], ids=["outside", "outside-one", "nested", "nested-three"]
)
def test_planted_list_heading_cannot_change_outer_section(indent):
    text = f"## Decision\n\n- See `tools/app.py`.\n{indent}## Context\n\nThe app retries.\n"
    found = records.blocks(text)
    tree = records.Tree(frozenset({"tools/app.py"}), "", frozenset())
    problems = [p for block in found for p in records.judge(NEW, block, tree)]
    assert problems == (
        [f"{NEW}:6: claim under 'Decision' {NO_POINTER}"] if len(indent) >= 2 else []
    )


@pytest.mark.parametrize("pointed", [False, True], ids=["split-pointer", "both-pointed"])
def test_planted_added_blank_binds_neighbouring_blocks(tree, pointed):
    repo = base_repo(tree)
    claim = "The app retries." + (" See `tools/app.py`." if pointed else "") + "\n"
    pointer = "See `tools/app.py`.\n"
    before = "## Decision\n\n" + claim + pointer
    repo.write({OLD: before})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "accepted pointed block")
    repo.write({OLD: before.replace(claim + pointer, claim + "\n" + pointer)})
    problems, changed, judged = records.check_records(repo.root, "main")
    assert (changed, judged) == (1, 2)
    assert problems == ([] if pointed else [f"{OLD}:3: claim under 'Decision' {NO_POINTER}"])
