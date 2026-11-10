# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Text-derived evidence anchors: fix and write tests."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import anchors
import pytest
from conftest import Tree
from test_anchors import PIN, URL, invoke, repository


def drifted_tree(tree: Tree, raw: str | None = None) -> tuple[Path, Path, bytes]:
    """Create one historical pin whose current source moved down a line."""
    if raw is None:
        raw = f'{{"one": "{URL}tools/a.py#L2-L3"}}\n'
    root = repository(tree, "header\nfirst\nlast\n", raw)
    tree.write({"tools/a.py": "new\nheader\nfirst\nlast\n"})
    path = root / ".bestpractices.json"
    return root, path, path.read_bytes()


def edit_after_moves(monkeypatch, path: Path, updated: bytes) -> None:
    """Plant an input edit after mapping and before the guarded replacement."""
    original = anchors._moves

    def edit(*args, **kwargs):
        """Return the original mapping after changing the selected input."""
        result = original(*args, **kwargs)
        path.write_bytes(updated)
        return result

    monkeypatch.setattr(anchors, "_moves", edit)


def test_planted_mapping_filters_pins_by_answer_key(tree: Tree) -> None:
    """Planted: historical pins sharing coordinates do not make other answers ambiguous."""
    raw = f'{{"one": "{URL}tools/a.py#L2", "two": "{URL}tools/a.py#L4"}}\n'
    root = repository(tree, "header\nalpha beta\n", raw)
    pins = (
        anchors.Pin(PIN.path, ("alpha",), ("alpha",), ("one",)),
        anchors.Pin(PIN.path, ("beta",), ("beta",), ("two",)),
    )
    tree.write({"tools/a.py": "new\nheader\nalpha\nbeta\n"})
    assert invoke(root, "fix", pins) == 0
    assert (root / ".bestpractices.json").read_bytes() == raw.replace("#L2", "#L3").encode()
    assert invoke(root, "check", pins) == 0


def test_planted_current_anchor_for_other_key_cannot_skip_mapping(
    tree: Tree,
    capsys,
) -> None:
    """Planted: another answer's valid destination cannot excuse an unmappable reference."""
    raw = f'{{"one": "{URL}tools/a.py#L3-L4", "two": "{URL}tools/a.py#L3-L4"}}\n'
    root, path, before = drifted_tree(tree, raw)
    assert invoke(root, "fix", (PIN,)) == 1
    assert (
        "anchor tools/a.py#L3-L4 for two cannot be mapped: matches 0 pins at HEAD"
        in capsys.readouterr().err
    )
    assert path.read_bytes() == before


def test_planted_shared_literal_with_different_destinations_is_refused(
    tree: Tree,
    capsys,
) -> None:
    """Planted: one literal cannot move to two answers' distinct destinations."""
    raw = f'{{"one": "{URL}tools/a.py#L2", "two": "{URL}tools/a.py#L2"}}\n'
    root = repository(tree, "header\nalpha beta\n", raw)
    path = root / ".bestpractices.json"
    before = path.read_bytes()
    pins = (
        anchors.Pin(PIN.path, ("alpha",), ("alpha",), ("one",)),
        anchors.Pin(PIN.path, ("beta",), ("beta",), ("two",)),
    )
    tree.write({"tools/a.py": "header\nalpha\nbeta\n"})
    assert invoke(root, "fix", pins) == 1
    assert (
        "anchor tools/a.py#L2 maps to both tools/a.py#L2 and tools/a.py#L3"
        in capsys.readouterr().err
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["ls-tree", "show"])
def test_planted_historical_read_failure_is_not_absence(
    tree: Tree,
    monkeypatch,
    capsys,
    operation: str,
) -> None:
    """Planted: failed tree or blob reads are errors even for a currently valid reference."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3", "two": "{URL}tools/a.py#L1"}}\n'
    root = repository(tree, "stable\nfirst\nlast\n", raw)
    pins = (PIN, anchors.Pin(PIN.path, ("stable",), ("stable",), ("two",)))
    tree.write({"tools/a.py": "stable\nnew\nfirst\nlast\n"})
    original = anchors._git

    def fail_read(root, *args):
        """Return an opaque read failure with no absence-related stderr keywords."""
        if args[0] == operation:
            return subprocess.CompletedProcess(["git"], 128, "", "opaque historical failure")
        return original(root, *args)

    monkeypatch.setattr(anchors, "_git", fail_read)
    assert invoke(root, "fix", pins) == 3
    assert "opaque historical failure" in capsys.readouterr().err
    assert (root / ".bestpractices.json").read_bytes() == raw.encode()


@pytest.mark.parametrize("raw", ["{}", '{"one": "No source link."}'])
def test_planted_fix_rechecks_unlinked_pin(tree: Tree, capsys, raw: str) -> None:
    """Planted: fix cannot report success while a declared pin remains unlinked."""
    tree.write({"tools/a.py": "first\nlast\n", ".bestpractices.json": raw})
    assert invoke(tree.root, "fix", (PIN,)) == 1
    output = capsys.readouterr()
    assert "after moves: pin tools/a.py#L1-L2 is not linked by one" in output.err
    assert "PASS" not in output.out
    assert (tree.root / ".bestpractices.json").read_bytes() == raw.encode()


def test_planted_fix_rechecks_unlinked_pin_after_a_move(tree: Tree, capsys) -> None:
    """Planted: a valid move cannot hide another answer's missing evidence."""
    root, path, before = drifted_tree(tree)
    pins = (PIN, anchors.Pin(PIN.path, ("header",), ("header",), ("two",)))
    assert invoke(root, "fix", pins) == 1
    assert "after moves: pin tools/a.py#L2 is not linked by two" in capsys.readouterr().err
    assert path.read_bytes() == before


@pytest.mark.parametrize("suffix", ["abc", "-L3x", "-L", "-L3-L4", ".x", ",x"])
def test_planted_malformed_anchor_is_refused(tree: Tree, capsys, suffix: str) -> None:
    """Planted: a numeric anchor prefix cannot validate an incomplete or extended token."""
    fragment = f"blob/main/tools/a.py#L2{suffix}"
    raw = f'{{"one": "{URL}tools/a.py#L2{suffix}"}}'
    tree.write({"tools/a.py": "header\nfirst\nlast\n", ".bestpractices.json": raw})
    pin = anchors.Pin(PIN.path, ("first",), ("first",), ("one",))
    assert invoke(tree.root, "check", (pin,)) == 1
    assert f"one: malformed anchor {fragment}" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", ["", " ", ")", ".", ",", ". next", ", next"])
def test_boundary_anchor_token_delimiters_are_accepted(tree: Tree, suffix: str) -> None:
    """Boundary: end, whitespace, closing parentheses and prose punctuation close anchors."""
    raw = f'{{"one": "{URL}tools/a.py#L2{suffix}"}}'
    tree.write({"tools/a.py": "header\nfirst\n", ".bestpractices.json": raw})
    pin = anchors.Pin(PIN.path, ("first",), ("first",), ("one",))
    assert invoke(tree.root, "check", (pin,)) == 0


@pytest.mark.parametrize("suffix", ["", " ", ")", ".", ",", ". next", ", next"])
def test_boundary_fix_uses_anchor_token_delimiters(tree: Tree, suffix: str) -> None:
    """Boundary: the same token boundaries govern checking and raw replacement."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3{suffix}"}}\n'
    root, path, before = drifted_tree(tree, raw)
    assert invoke(root, "fix", (PIN,)) == 0
    assert path.read_bytes() == before.replace(b"#L2-L3", b"#L3-L4")
    assert invoke(root, "check", (PIN,)) == 0


def test_planted_last_block_cannot_end_inside_first_block() -> None:
    """Planted: a short ending block cannot truncate the longer starting block."""
    pin = anchors.Pin(PIN.path, ("a", "b", "c"), ("b",), ("one",))
    assert anchors.derive(pin, ["a", "b", "c", "b"]) == (1, 4)
    assert anchors.derive(pin, ["a", "b", "c"]) == (
        "pin tools/a.py last text 'b' matches no place at or after line 1"
    )


def test_boundary_last_block_can_end_at_first_block_end() -> None:
    """Boundary: both historical and current ranges can end at the first block's end."""
    pin = anchors.Pin(PIN.path, ("a", "b", "c"), ("c",), ("one",))
    assert anchors.derive(pin, ["a", "b", "c"]) == (1, 3)
    assert anchors._maps_to(pin, ["a", "b", "c"], 1, 3)
    assert not anchors._maps_to(pin, ["a", "b", "c"], 1, 2)


@pytest.mark.parametrize("raw", ["[]", '{"a": 1}', "null", '"answer"'])
@pytest.mark.parametrize("command", ["check", "fix"])
def test_planted_non_text_answer_mapping_is_file_error(
    tree: Tree,
    capsys,
    raw: str,
    command: str,
) -> None:
    """Planted: syntactically valid JSON must still map answer names to text."""
    tree.write({".bestpractices.json": raw})
    assert invoke(tree.root, command, ()) == 3
    assert capsys.readouterr().err == "ERROR: .bestpractices.json must map answer names to text\n"
    assert (tree.root / ".bestpractices.json").read_bytes() == raw.encode()


def test_boundary_empty_text_answer_mapping_is_valid(tree: Tree) -> None:
    """Boundary: an empty mapping or empty string value is valid without declared pins."""
    for raw in ("{}", '{"one": ""}'):
        tree.write({".bestpractices.json": raw})
        assert invoke(tree.root, "check", ()) == 0
        assert invoke(tree.root, "fix", (), "--base", "missing-revision") == 0


def test_planted_missing_pin_source_is_reported(tree: Tree) -> None:
    """Planted: a missing pin source is reported even without any linked answers."""
    pin = anchors.Pin("tools/gone.py", ("first",), ("last",), ("one",))
    assert anchors.findings({}, tree.root, (pin,)) == ["pin tools/gone.py source file is missing"]


def test_boundary_present_pin_source_is_derived(tree: Tree) -> None:
    """Boundary: a present source still derives its expected complete range."""
    tree.write({"tools/a.py": "first\nlast\n"})
    assert anchors._ranges(tree.root, (PIN,)) == {PIN: (1, 2)}


@pytest.mark.parametrize("operation", ["replace", "fsync"])
def test_planted_atomic_write_failure_preserves_original(
    tree: Tree,
    monkeypatch,
    capsys,
    operation: str,
) -> None:
    """Planted: failed flush or replacement preserves bytes and removes its temporary file."""
    root, path, before = drifted_tree(tree)
    entries = set(root.iterdir())

    def fail(*args, **kwargs):
        """Plant an operating-system failure at the selected write stage."""
        raise OSError(f"planted {operation} failure")

    monkeypatch.setattr(anchors.os, operation, fail)
    assert invoke(root, "fix", (PIN,)) == 3
    assert f"planted {operation} failure" in capsys.readouterr().err
    assert path.read_bytes() == before
    assert set(root.iterdir()) == entries


def test_planted_cleanup_failure_keeps_replace_error(tree: Tree, monkeypatch, capsys) -> None:
    """Planted: failed cleanup reports itself without hiding the replacement error."""
    root, path, before = drifted_tree(tree)

    def fail_replace(*args, **kwargs):
        """Plant a replacement failure before the answer bytes change."""
        raise OSError("planted replace failure")

    def fail_unlink(*args, **kwargs):
        """Plant a cleanup failure while the replacement error propagates."""
        raise OSError("planted unlink failure")

    monkeypatch.setattr(anchors.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    assert invoke(root, "fix", (PIN,)) == 3
    stderr = capsys.readouterr().err
    assert "planted replace failure" in stderr
    assert "cannot remove temporary file" in stderr
    assert "planted unlink failure" in stderr
    assert path.read_bytes() == before


def test_boundary_cleanup_failure_alone_is_reported(tree: Tree, monkeypatch, capsys) -> None:
    """Boundary: cleanup failure after successful replacement still reports an error."""
    root, path, before = drifted_tree(tree)

    def fail_unlink(*args, **kwargs):
        """Plant cleanup failure even though replacement already renamed the temporary."""
        raise OSError("planted unlink failure")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    assert invoke(root, "fix", (PIN,)) == 3
    stderr = capsys.readouterr().err
    assert "cannot remove temporary file" in stderr
    assert "planted unlink failure" in stderr
    # Replacement succeeds before cleanup fails, so the moved anchors remain on disk.
    assert path.read_bytes() == before.replace(b"#L2-L3", b"#L3-L4")


def test_boundary_atomic_write_preserves_mode_and_cleans_temp(tree: Tree) -> None:
    """Boundary: a successful replacement preserves the original permission bits."""
    root, path, before = drifted_tree(tree)
    path.chmod(0o640)
    mode = stat.S_IMODE(path.stat().st_mode)
    entries = set(root.iterdir())
    assert invoke(root, "fix", (PIN,)) == 0
    assert path.read_bytes() == before.replace(b"#L2-L3", b"#L3-L4")
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert set(root.iterdir()) == entries


def test_planted_escaped_raw_anchor_refuses_fake_move(tree: Tree, capsys) -> None:
    """Planted: a decoded anchor hidden behind slash escapes cannot report an unwritten move."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3"}}\n'.replace("/", r"\/")
    root, path, before = drifted_tree(tree, raw)
    assert invoke(root, "fix", (PIN,)) == 1
    output = capsys.readouterr()
    assert "anchor moves do not apply to the raw file (escaped spelling?)" in output.err
    assert "moved " not in output.out
    assert path.read_bytes() == before


def test_boundary_unrelated_json_escape_preserves_spelling(tree: Tree) -> None:
    """Boundary: unrelated escapes do not prevent an otherwise exact anchor move."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3", "note": "slash \\/"}}\n'
    root, path, before = drifted_tree(tree, raw)
    assert invoke(root, "fix", (PIN,)) == 0
    assert path.read_bytes() == before.replace(b"#L2-L3", b"#L3-L4")


def test_planted_concurrent_answer_edit_is_preserved(
    tree: Tree,
    monkeypatch,
    capsys,
) -> None:
    """Planted: an edit made after mapping survives a refused answer replacement."""
    root, path, before = drifted_tree(tree)
    edited = before + b" "
    entries = set(root.iterdir())
    edit_after_moves(monkeypatch, path, edited)
    assert invoke(root, "fix", (PIN,)) == 1
    assert ".bestpractices.json changed while fixing" in capsys.readouterr().err
    assert path.read_bytes() == edited
    assert set(root.iterdir()) == entries


def test_planted_concurrent_source_edit_refuses_stale_anchor(
    tree: Tree,
    monkeypatch,
    capsys,
) -> None:
    """Planted: a source edit after derivation prevents stale answer bytes from landing."""
    root, path, before = drifted_tree(tree)
    source = root / PIN.path
    edited = b"another insertion\n" + source.read_bytes()
    entries = set(root.iterdir())
    edit_after_moves(monkeypatch, source, edited)
    assert invoke(root, "fix", (PIN,)) == 1
    assert "tools/a.py changed while fixing" in capsys.readouterr().err
    assert path.read_bytes() == before
    assert source.read_bytes() == edited
    assert set(root.iterdir()) == entries


def test_boundary_source_snapshot_is_shared_until_final_readback(
    tree: Tree,
    monkeypatch,
) -> None:
    """Boundary: derivation, mapping and validation share one read before the final guard."""
    raw = f'{{"one": "{URL}tools/a.py#L2-L3", "two": "{URL}tools/a.py#L2"}}\n'
    root, path, before = drifted_tree(tree, raw)
    pins = (PIN, anchors.Pin(PIN.path, ("header",), ("header",), ("two",)))
    original = anchors._source_text
    reads = []

    def record(source):
        """Record source reads without changing their contents."""
        reads.append(source)
        return original(source)

    monkeypatch.setattr(anchors, "_source_text", record)
    assert invoke(root, "fix", pins) == 0
    assert reads == [root / PIN.path, root / PIN.path]
    assert path.read_bytes() == before.replace(b"#L2-L3", b"#L3-L4")


def test_planted_base_name_is_resolved_once_for_all_reads(tree: Tree, monkeypatch) -> None:
    """Planted: tree and blob reads use the resolved OID even if the base name moves."""
    root, path, before = drifted_tree(tree)
    original = anchors._git
    calls = []
    resolved = []

    def moving_base(root, *args):
        """Reject repeated symbolic-base resolution after the first successful lookup."""
        calls.append(args)
        if args[0] == "rev-parse":
            done = original(root, *args)
            resolved.append(done.stdout.strip())
            return done
        assert resolved
        assert "HEAD" not in " ".join(args)
        return original(root, *args)

    monkeypatch.setattr(anchors, "_git", moving_base)
    assert invoke(root, "fix", (PIN,)) == 0
    assert calls == [
        ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
        ("ls-tree", "--name-only", resolved[0], "--", PIN.path),
        ("show", f"{resolved[0]}:{PIN.path}"),
    ]
    assert path.read_bytes() == before.replace(b"#L2-L3", b"#L3-L4")


def test_boundary_mapping_problem_keeps_user_base_name(tree: Tree, capsys) -> None:
    """Boundary: a diagnostic retains the requested revision instead of its resolved OID."""
    raw = f'{{"one": "{URL}tools/a.py#L1"}}\n'
    root, path, before = drifted_tree(tree, raw)
    assert invoke(root, "fix", (PIN,), "--base", "main") == 1
    assert (
        "anchor tools/a.py#L1 for one cannot be mapped: matches 0 pins at main"
        in capsys.readouterr().err
    )
    assert path.read_bytes() == before


def test_planted_git_read_explicitly_uses_utf8(tree: Tree, monkeypatch) -> None:
    """Planted: git output must be decoded as UTF-8 with bounded execution."""
    calls = []

    def record(args, **kwargs):
        """Capture subprocess options without launching git or reading configuration."""
        calls.append(kwargs)
        return subprocess.CompletedProcess(["git"], 0, "π\n", "")

    monkeypatch.setattr(anchors.ciwf, "git_env", dict)
    monkeypatch.setattr(anchors.subprocess, "run", record)
    assert anchors._git(tree.root, "show", "base:tools/π.py").stdout == "π\n"
    assert calls == [
        {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "check": False,
            "timeout": 30,
            "env": {},
        }
    ]
