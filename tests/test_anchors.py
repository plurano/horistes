# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Text-derived evidence anchors: drift, historical mapping and exact edits."""

from __future__ import annotations

import subprocess
from pathlib import Path

import anchors
import pytest
from conftest import Tree

URL = "https://github.com/plurano/horistes/blob/main/"
PIN = anchors.Pin("tools/a.py", ("first",), ("last",), ("one",))


def repository(tree: Tree, source: str, raw: str) -> Path:
    """Commit a fixture's historical source and exact answer bytes."""
    tree.write({"tools/a.py": source, ".bestpractices.json": raw})
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "base")
    return tree.root


def invoke(root: Path, command: str, pins: tuple[anchors.Pin, ...], *args: str) -> int:
    """Use the CLI's parsing and error reporting with fixture-specific pins."""
    return anchors.main([command, "--root", str(root), *args], pins=pins)


def test_planted_drift_moves_repeated_anchors_only(tree: Tree, capsys) -> None:
    """Planted: check refuses drift; fix preserves every byte except line numbers."""
    raw = (
        "{\r\n"
        f'\t"one": "Evidence: {URL}tools/a.py#L2-L3",\r\n'
        f'\t"two" : "{URL}tools/a.py#L2-L3", "note": "π"\r\n'
        "}\r\n"
    )
    root = repository(tree, "header\nfirst\nlast\n", raw)
    before = (root / ".bestpractices.json").read_bytes()
    pins = (anchors.Pin(PIN.path, PIN.first, PIN.last, ("one", "two")),)
    tree.write({"tools/a.py": "inserted\ninserted again\nheader\nfirst\nlast\n"})
    assert invoke(root, "check", pins) == 1
    assert "matches no pin" in capsys.readouterr().err
    assert invoke(root, "fix", pins) == 0
    assert capsys.readouterr().out == "moved tools/a.py#L2-L3 -> tools/a.py#L4-L5\n"
    assert (root / ".bestpractices.json").read_bytes() == before.replace(b"#L2-L3", b"#L4-L5")
    assert invoke(root, "check", pins) == 0


def test_planted_unmappable_anchor_writes_nothing(tree: Tree, capsys) -> None:
    """Planted: a valid move followed by an unknown old range leaves all bytes intact."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3 {URL}tools/a.py#L1"}}\n'
    root = repository(tree, "header\nfirst\nlast\n", raw)
    before = (root / ".bestpractices.json").read_bytes()
    tree.write({"tools/a.py": "inserted\nheader\nfirst\nlast\n"})
    assert invoke(root, "fix", (PIN,)) == 1
    assert "anchor tools/a.py#L1 cannot be mapped: matches 0 pins" in capsys.readouterr().err
    assert (root / ".bestpractices.json").read_bytes() == before


def test_boundary_no_drift_leaves_bytes_unchanged(tree: Tree, capsys) -> None:
    """Boundary: fix needs no git lookup when every anchor already matches."""
    raw = f'{{ "one" : "{URL}tools/a.py#L2-L3" }}\n'
    root = repository(tree, "header\nfirst\nlast\n", raw)
    before = (root / ".bestpractices.json").read_bytes()
    assert invoke(root, "fix", (PIN,), "--base", "missing-revision") == 0
    assert capsys.readouterr().out == "PASS: no anchor to move\n"
    assert (root / ".bestpractices.json").read_bytes() == before


def test_boundary_short_anchor_does_not_replace_longer_anchor(tree: Tree) -> None:
    """Boundary: moving L2 preserves L23 and separately maps the range L2-L3."""
    raw = f'{{"one": "{URL}tools/a.py#L2 {URL}tools/a.py#L2-L3 {URL}tools/a.py#L23"}}\n'
    lines = ["header", "first", "last", *(["padding"] * 19), "stable"]
    root = repository(tree, "\n".join(lines) + "\n", raw)
    before = (root / ".bestpractices.json").read_bytes()
    pins = (
        anchors.Pin("tools/a.py", ("first",), ("first",), ("one",)),
        PIN,
        anchors.Pin("tools/a.py", ("stable",), ("stable",), ("one",)),
    )
    tree.write({"tools/a.py": "\n".join(["new", "new", *lines[:3], *lines[5:]]) + "\n"})
    assert invoke(root, "fix", pins) == 0
    expected = before.replace(b"#L2 ", b"#L4 ").replace(b"#L2-L3", b"#L4-L5")
    assert (root / ".bestpractices.json").read_bytes() == expected
    assert invoke(root, "check", pins) == 0


def test_boundary_moves_do_not_cascade(tree: Tree) -> None:
    """Boundary: one move's destination can also be another move's old range."""
    raw = f'{{"one": "{URL}tools/a.py#L2 {URL}tools/a.py#L4"}}\n'
    root = repository(tree, "header\nfirst\nmiddle\nlast\n", raw)
    tree.write({"tools/a.py": "new\nnew\nheader\nfirst\nmiddle\nlast\n"})
    pins = (
        anchors.Pin("tools/a.py", ("first",), ("first",), ("one",)),
        anchors.Pin("tools/a.py", ("last",), ("last",), ("one",)),
    )
    assert invoke(root, "fix", pins) == 0
    expected = raw.replace("#L4", "#L6").replace("#L2", "#L4")
    assert (root / ".bestpractices.json").read_bytes() == expected.encode("utf-8")


def test_planted_swapped_pins_keep_answer_bindings(tree: Tree, capsys) -> None:
    """Planted: swapping two valid ranges must move each answer's own evidence."""
    raw = f'{{"one": "{URL}tools/a.py#L2", "two": "{URL}tools/a.py#L3"}}\n'
    root = repository(tree, "header\nalpha\nbeta\n", raw)
    pins = (
        anchors.Pin("tools/a.py", ("alpha",), ("alpha",), ("one",)),
        anchors.Pin("tools/a.py", ("beta",), ("beta",), ("two",)),
    )
    tree.write({"tools/a.py": "header\nbeta\nalpha\n"})
    assert invoke(root, "check", pins) == 1
    errors = capsys.readouterr().err
    assert "one: anchor tools/a.py#L2 matches no pin for this answer" in errors
    assert "pin tools/a.py#L3 is not linked by one" in errors
    assert invoke(root, "fix", pins) == 0
    expected = f'{{"one": "{URL}tools/a.py#L3", "two": "{URL}tools/a.py#L2"}}\n'
    assert (root / ".bestpractices.json").read_bytes() == expected.encode("utf-8")
    assert invoke(root, "check", pins) == 0


def test_boundary_file_absent_at_base_keeps_current_anchor(tree: Tree, capsys) -> None:
    """Boundary: a new file's correct anchor survives a move in an older file."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3", "two": "{URL}tools/new.py#L2"}}\n'
    root = repository(tree, "header\nfirst\nlast\n", raw)
    pins = (PIN, anchors.Pin("tools/new.py", ("new evidence",), ("new evidence",), ("two",)))
    tree.write(
        {
            "tools/a.py": "inserted\nheader\nfirst\nlast\n",
            "tools/new.py": "header\nnew evidence\n",
        }
    )
    assert invoke(root, "fix", pins) == 0
    assert capsys.readouterr().out == "moved tools/a.py#L2-L3 -> tools/a.py#L3-L4\n"
    expected = raw.replace("#L2-L3", "#L3-L4")
    assert (root / ".bestpractices.json").read_bytes() == expected.encode("utf-8")
    assert invoke(root, "check", pins) == 0


def test_boundary_unmoved_anchor_is_not_reported(tree: Tree, capsys) -> None:
    """Boundary: fix names and rewrites only anchors whose lines changed."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3", "two": "{URL}tools/a.py#L1"}}\n'
    root = repository(tree, "stable\nfirst\nlast\n", raw)
    pins = (PIN, anchors.Pin("tools/a.py", ("stable",), ("stable",), ("two",)))
    tree.write({"tools/a.py": "stable\ninserted\nfirst\nlast\n"})
    assert invoke(root, "fix", pins) == 0
    assert capsys.readouterr().out == "moved tools/a.py#L2-L3 -> tools/a.py#L3-L4\n"
    expected = raw.replace("#L2-L3", "#L3-L4")
    assert (root / ".bestpractices.json").read_bytes() == expected.encode("utf-8")
    assert invoke(root, "check", pins) == 0


@pytest.mark.parametrize("raw", ["{}", '{"one": "No source link."}'])
def test_planted_pin_answer_does_not_link_evidence(tree: Tree, capsys, raw: str) -> None:
    """Planted: a declared answer must exist and link its pin's range."""
    tree.write({"tools/a.py": "header\nfirst\n", ".bestpractices.json": raw})
    pin = anchors.Pin("tools/a.py", ("first",), ("first",), ("one",))
    assert invoke(tree.root, "check", (pin,)) == 1
    assert "pin tools/a.py#L2 is not linked by one" in capsys.readouterr().err


def test_planted_pin_lists_no_answer(tree: Tree, capsys) -> None:
    """Planted: a pin without answer keys cannot declare required evidence."""
    tree.write({"tools/a.py": "header\nfirst\nlast\n", ".bestpractices.json": "{}"})
    pin = anchors.Pin(PIN.path, PIN.first, PIN.last, ())
    assert invoke(tree.root, "check", (pin,)) == 1
    assert "pin tools/a.py lists no answer" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", ["3", "-L3"])
def test_boundary_pin_requires_whole_anchor_match(tree: Tree, capsys, suffix: str) -> None:
    """Boundary: neither L23 nor L2-L3 counts as a link to L2."""
    raw = f'{{"one": "{URL}tools/a.py#L2{suffix}"}}'
    tree.write({"tools/a.py": "header\nfirst\nlast\n", ".bestpractices.json": raw})
    pin = anchors.Pin("tools/a.py", ("first",), ("first",), ("one",))
    assert invoke(tree.root, "check", (pin,)) == 1
    errors = capsys.readouterr().err
    assert f"one: anchor tools/a.py#L2{suffix} matches no pin for this answer" in errors
    assert "pin tools/a.py#L2 is not linked by one" in errors


def test_planted_missing_base_is_git_error(tree: Tree, capsys) -> None:
    """Planted: a missing historical revision is an error and never writes."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3"}}\n'
    root = repository(tree, "header\nfirst\nlast\n", raw)
    before = (root / ".bestpractices.json").read_bytes()
    tree.write({"tools/a.py": "new\nheader\nfirst\nlast\n"})
    assert invoke(root, "fix", (PIN,), "--base", "missing-revision") == 3
    assert "ERROR: base revision missing-revision not found" in capsys.readouterr().err
    assert (root / ".bestpractices.json").read_bytes() == before


def test_planted_ambiguous_base_pin_writes_nothing(tree: Tree, capsys) -> None:
    """Planted: several pins matching the old boundaries prevent a guessed move."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3"}}\n'
    root = repository(tree, "header\nfirst\nlast\n", raw)
    before = (root / ".bestpractices.json").read_bytes()
    tree.write({"tools/a.py": "new\nheader\nfirst\nlast\n"})
    assert invoke(root, "fix", (PIN, PIN)) == 1
    assert "matches 2 pins" in capsys.readouterr().err
    assert (root / ".bestpractices.json").read_bytes() == before


def test_planted_unmappable_current_pin_writes_nothing(tree: Tree, capsys) -> None:
    """Planted: a valid historical pin with a missing current block cannot move."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3"}}\n'
    root = repository(tree, "header\nfirst\nlast\n", raw)
    before = (root / ".bestpractices.json").read_bytes()
    tree.write({"tools/a.py": "header\nfirst\ngone\n"})
    assert invoke(root, "fix", (PIN,)) == 1
    assert "cannot be mapped: pin tools/a.py last text" in capsys.readouterr().err
    assert (root / ".bestpractices.json").read_bytes() == before


def test_planted_missing_current_file_writes_nothing(tree: Tree, capsys) -> None:
    """Planted: an anchor into a deleted source file cannot move and needs no git read."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3"}}\n'
    root = repository(tree, "header\nfirst\nlast\n", raw)
    before = (root / ".bestpractices.json").read_bytes()
    (root / "tools/a.py").unlink()
    assert invoke(root, "fix", (PIN,), "--base", "missing-revision") == 1
    assert (
        "anchor tools/a.py#L2-L3 cannot be mapped: tools/a.py is missing" in capsys.readouterr().err
    )
    assert (root / ".bestpractices.json").read_bytes() == before


@pytest.mark.parametrize("source", ["{", "\xff"])
def test_planted_bad_answers_are_file_errors(tree: Tree, source: str, capsys) -> None:
    """Planted: malformed JSON and invalid UTF-8 report the CLI error code."""
    path = tree.root / ".bestpractices.json"
    path.write_bytes(source.encode("latin-1"))
    assert invoke(tree.root, "check", ()) == 3
    assert capsys.readouterr().err.startswith("ERROR:")


@pytest.mark.parametrize(
    "failure", [OSError("git unavailable"), subprocess.TimeoutExpired("git", 30)]
)
def test_planted_git_failure_is_error(tree: Tree, monkeypatch, capsys, failure: Exception) -> None:
    """Planted: launch failure or timeout propagates without editing the answers."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3"}}\n'
    tree.write({"tools/a.py": "new\nheader\nfirst\nlast\n", ".bestpractices.json": raw})
    before = (tree.root / ".bestpractices.json").read_bytes()

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(anchors.ciwf, "git_env", dict)
    monkeypatch.setattr(anchors.subprocess, "run", fail)
    assert invoke(tree.root, "fix", (PIN,)) == 3
    assert capsys.readouterr().err.startswith("ERROR:")
    assert (tree.root / ".bestpractices.json").read_bytes() == before


def test_derive_unique_match() -> None:
    """A unique first substring derives the earliest matching ending line."""
    assert anchors.derive(PIN, ["header", "  first", "middle", "last", "last"]) == (2, 4)


@pytest.mark.parametrize("lines, count", [(["gone", "last"], 0), (["first", "first", "last"], 2)])
def test_derive_refuses_missing_or_ambiguous_first(lines: list[str], count: int) -> None:
    """Planted: first text must occur exactly once."""
    assert anchors.derive(PIN, lines) == f"pin tools/a.py first text 'first' matches {count} places"


def test_derive_multiline_blocks() -> None:
    """Consecutive blocks disambiguate individual repeated lines and include the whole end."""
    pin = anchors.Pin("tools/a.py", ("first", "next"), ("end", "last"), ("one",))
    lines = ["first", "wrong", "first", "next", "end", "wrong", "end", "last"]
    assert anchors.derive(pin, lines) == (3, 8)


def test_derive_single_line() -> None:
    """Boundary: the same one-line block yields a single-line range."""
    pin = anchors.Pin("tools/a.py", ("same",), ("same",), ("one",))
    assert anchors.derive(pin, ["header", "same"]) == (2, 2)
    assert anchors.anchor_text(pin.path, 2, 2) == "tools/a.py#L2"


def test_derive_last_block_cannot_start_before_first() -> None:
    """Planted: an ending block overlapping earlier lines cannot close the range."""
    pin = anchors.Pin("tools/a.py", ("first",), ("earlier", "first"), ("one",))
    assert anchors.derive(pin, ["earlier", "first"]) == (
        "pin tools/a.py last text 'first' matches no place at or after line 2"
    )


def test_derive_incomplete_blocks_do_not_wrap() -> None:
    """Planted: the first and last blocks must fit completely before EOF."""
    pin = anchors.Pin("tools/a.py", ("first", "next"), ("last",), ("one",))
    assert anchors.derive(pin, ["first"]) == "pin tools/a.py first text 'first' matches 0 places"
    pin = anchors.Pin("tools/a.py", ("first",), ("last", "missing"), ("one",))
    assert anchors.derive(pin, ["first", "last"]) == (
        "pin tools/a.py last text 'missing' matches no place at or after line 1"
    )


@pytest.mark.parametrize("first, last", [((), ("last",)), (("first",), ())])
def test_derive_refuses_empty_blocks(first: tuple[str, ...], last: tuple[str, ...]) -> None:
    """Planted: an empty block cannot delimit source evidence."""
    with pytest.raises(ValueError, match="needs nonempty first and last blocks"):
        anchors.derive(anchors.Pin("tools/a.py", first, last, ("one",)), ["first", "last"])
