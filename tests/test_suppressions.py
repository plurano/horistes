# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Checks on inline suppressions: nosemgrep and nosec must give rule IDs and reasons."""

from __future__ import annotations

import io
import os
import re
import tokenize
from pathlib import Path

import pytest
import yaml
from conftest import ROOT

NOSEMGREP_DIRECTIVE = re.compile(r"\bnosemgrep\b")
NOSEMGREP_VALID = re.compile(
    r"^#\s*nosemgrep:\s*[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*\s+[^,\n]*[A-Za-z][^,\n]*$"
)

NOSEC_DIRECTIVE = re.compile(r"\bnosec\b")
NOSEC_VALID = re.compile(r"^#\s*nosec\s+B\d{3}(?:[ ,]+B\d{3})*\s*#\s*[^#\n]*[A-Za-z][^#\n]*$")

PRUNE_DIRS = {
    ".claude",
    ".git",
    ".lane-input",
    ".venv",
    ".workingdir",
    "__pycache__",
    "node_modules",
}

PLANTED_REFUSALS: list[tuple[str, str, str]] = [
    ("bare-nosemgrep", "# nosemgrep\n", "code"),
    ("nosemgrep-no-reason", "# nosemgrep: rule-id\n", "code"),
    ("nosemgrep-comma-reason", "# nosemgrep: rule-id bad, reason\n", "code"),
    ("bare-nosec", "# nosec\n", "code"),
    ("nosec-no-reason", "# nosec B101\n", "code"),
    ("nosec-reason-no-id", "# nosec # reason without id\n", "code"),
    (
        "engine-exclude",
        "engines:\n  opengrep:\n    exclude_paths:\n      - 'tests/a.py'\n",
        "yaml",
    ),
]

ACCEPTED_BOUNDARIES: list[tuple[str, str]] = [
    ("nosemgrep-one-id", "# nosemgrep: rule-id valid test reason\n"),
    ("nosemgrep-two-ids", "# nosemgrep: rule-one, rule-two valid test reason\n"),
    ("nosec-two-ids", "# nosec B101, B102 # valid test reason\n"),
    ("string-literal", 'TEXT = "# nosemgrep bare inside string literal"\n'),
]


def suppression_problems(text: str, name: str) -> list[str]:
    """Tokenize text and verify that all suppression comments meet requirements."""
    problems: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            line = tok.start[0]
            comment = tok.string.strip()
            if NOSEMGREP_DIRECTIVE.search(comment) and not NOSEMGREP_VALID.match(comment):
                problems.append(f"{name}:{line}: invalid nosemgrep suppression: {comment}")
            if NOSEC_DIRECTIVE.search(comment) and not NOSEC_VALID.match(comment):
                problems.append(f"{name}:{line}: invalid nosec suppression: {comment}")
    except tokenize.TokenError as err:
        problems.append(f"{name}:1: tokenize error: {err}")
    return problems


def codacy_problems(text: str) -> list[str]:
    """Check .codacy.yml: engines must not define exclude_paths."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return []
    engines = data.get("engines")
    if not isinstance(engines, dict):
        return []
    problems: list[str] = []
    for engine, config in sorted(engines.items()):
        if isinstance(config, dict) and "exclude_paths" in config:
            problems.append(f"engines.{engine}.exclude_paths is not permitted")
    return problems


def tree_problems(root: Path) -> list[str]:
    """Walk root finding problems in all Python files, pruning ignored directories."""
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
        problems.extend(suppression_problems(text, rel))
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
    problems = suppression_problems(payload, "test.py")
    assert len(problems) == 1
    assert problems[0].startswith("test.py:1: invalid ")


@pytest.mark.parametrize(
    "payload",
    [payload for _, payload in ACCEPTED_BOUNDARIES],
    ids=[case_id for case_id, _ in ACCEPTED_BOUNDARIES],
)
def test_accepted_boundary_rows(payload: str) -> None:
    """Boundary conditions: valid suppressions and comments in string literals pass."""
    assert suppression_problems(payload, "test.py") == []
