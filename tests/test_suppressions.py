# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Checks on inline suppressions: opengrep and Bandit ignore comments give rule IDs and reasons."""

from __future__ import annotations

import datetime
import os
from pathlib import Path

import exceptions
import pytest
from conftest import ROOT
from exceptions import WORDS, codacy_problems, suppression_problems

PRUNE_DIRS = {
    ".claude",
    ".git",
    ".lane-input",
    ".venv",
    ".workingdir",
    "__pycache__",
    "node_modules",
}

# Payloads name the ignore words as {sem}, {sg}, {SG} and {og}; planted() fills them in.
PLANTED_REFUSALS: list[tuple[str, str, str]] = [
    ("bare-ignore-word", "# {sg}\n", "code"),
    ("ignore-word-no-reason", "# {sg}: rule-id\n", "code"),
    (
        "ignore-word-comma-reason",
        "# {sg}: rule-id expires 2026-11-12 category B: bad, reason\n",
        "code",
    ),
    ("bare-nosec", "# nosec\n", "code"),
    ("nosec-no-reason", "# nosec B101\n", "code"),
    ("nosec-reason-no-id", "# nosec # reason without id\n", "code"),
    ("short-alias", "# {sem}\n", "code"),
    (
        "opengrep-alias",
        "# {og}: rule-id expires 2026-11-12 category B: valid test reason\n",
        "code",
    ),
    (
        "upper-case-ignore-word",
        "# {SG}: rule-id expires 2026-11-12 category B: valid test reason\n",
        "code",
    ),
    ("word-first-prose", "# {sg} is not wanted here\n", "code"),
    ("prose-after-code", "run(cmd)  # we avoid {sg} here\n", "code"),
    ("string-on-code-line", 'run(cmd, env={{"NOTE": "{sg}"}})\n', "code"),
    ("string-literal", 'TEXT = "# {sg}: rule-id valid test reason"\n', "code"),
    ("fstring-part", 'TEXT = f"{{cmd}} {sg}"\n', "code"),
    ("identifier", "def run(cmd, {sem}):\n    pass\n", "code"),
    ("nosec-in-longer-word", "x = 1  # nosecurity\n", "code"),
    ("nosec-after-second-hash", "x = 1  # note # nosec\n", "code"),
    (
        "engine-exclude",
        "engines:\n  opengrep:\n    exclude_paths:\n      - 'tests/a.py'\n",
        "yaml",
    ),
]

ACCEPTED_BOUNDARIES: list[tuple[str, str]] = [
    ("ignore-word-one-id", "# {sg}: rule-id expires 2026-11-12 category B: valid test reason\n"),
    (
        "ignore-word-two-ids",
        "# {sg}: rule-one, rule-two expires 2026-11-12 category B: valid test reason\n",
    ),
    ("nosec-two-ids", "# nosec B101, B102 expires 2026-11-12 category B: valid test reason\n"),
    ("split-word-string", 'TEXT = "no" + "semgrep"\n'),
    ("nosec-in-string", 'TEXT = "# nosec"\n'),
    ("own-line-prose", "# This helper explains why {sg} is not used here.\n"),
    ("nosec-prose", "x = 1  # see the nosec docs before adding one\n"),
    ("nosec-upper-case", "x = 1  # NOSEC\n"),
]


def planted(payload: str) -> str:
    """A planted payload with the ignore words filled in."""
    return payload.format(**WORDS)


def tree_problems(root: Path) -> list[str]:
    """Walk root finding problems in all Python files, pruning ignored directories."""
    today = datetime.date(2026, 11, 9)
    _, _, valid_map = exceptions._own(root, today)
    valid_exceptions = set(valid_map.keys())
    problems: list[str] = []
    py_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                py_files.append(Path(dirpath) / filename)
    for py_file in sorted(py_files):
        rel = py_file.relative_to(root).as_posix()
        text = py_file.read_text(encoding="utf-8")
        probs = suppression_problems(text, rel, today=today, valid_exceptions=valid_exceptions)
        problems.extend(probs)
    return problems


def test_committed_tree_has_no_problems() -> None:
    """The committed Python source tree contains no suppression problems."""
    assert tree_problems(ROOT) == []


def test_committed_codacy_yml_has_no_problems() -> None:
    """The committed .codacy.yml contains no engines exclude_paths entries."""
    text = (ROOT / ".codacy.yml").read_text(encoding="utf-8")
    assert codacy_problems(text) == []


@pytest.mark.parametrize(
    ("payload", "kind"),
    [(payload, kind) for _, payload, kind in PLANTED_REFUSALS],
    ids=[case_id for case_id, _, _ in PLANTED_REFUSALS],
)
def test_planted_refusals(payload: str, kind: str) -> None:
    """Planted defect: invalid suppression or engine exclude_paths produces a problem."""
    if kind == "yaml":
        assert codacy_problems(payload) == ["engines.opengrep.exclude_paths is not permitted"]
        return
    problems = suppression_problems(planted(payload), "test.py")
    assert len(problems) == 1
    assert problems[0].startswith("test.py:1: invalid ")


@pytest.mark.parametrize(
    "payload",
    [payload for _, payload in ACCEPTED_BOUNDARIES],
    ids=[case_id for case_id, _ in ACCEPTED_BOUNDARIES],
)
def test_accepted_boundary_rows(payload: str) -> None:
    """Boundary conditions: valid suppressions, split words and nosec in strings pass."""
    assert suppression_problems(planted(payload), "test.py") == []


def test_ignore_word_in_multiline_string_names_its_line() -> None:
    """Boundary: an ignore word on the third line of a string is refused at that line.

    Opengrep reads that line as the line above the next statement's finding.
    """
    problems = suppression_problems(planted('NOTE = """first\nsecond\n    {sg}"""\n'), "test.py")
    assert len(problems) == 1
    assert problems[0].startswith("test.py:3: invalid opengrep ignore word outside a comment: ")
