# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Checks on inline suppressions: opengrep and Bandit ignore comments give rule IDs and reasons."""

from __future__ import annotations

import io
import os
import re
import tokenize
from pathlib import Path

import pytest
import yaml
from conftest import ROOT

# Opengrep reads raw source lines, strings and names included, so this file never
# spells an ignore word whole outside a comment: it joins each from two halves.
SHORT_WORD = "no" + "sem"
LONG_WORD = SHORT_WORD + "grep"
OPEN_WORD = "no" + "opengrep"
WORDS = {"sem": SHORT_WORD, "sg": LONG_WORD, "SG": LONG_WORD.upper(), "og": OPEN_WORD}

# Opengrep 1.30.0 (Nosemgrep.ml) takes nosem, nosemgrep and noopengrep, in any
# case, as one ignore word. On a line with code it reads the word anywhere in the
# line; on the line above a finding, only when no letter or digit comes first.
OPENGREP_IGNORE_WORD = r"\b(?:" + "|".join((SHORT_WORD, LONG_WORD, OPEN_WORD)) + r")\b"
OPENGREP_AFTER_CODE = re.compile(OPENGREP_IGNORE_WORD, re.IGNORECASE)
OPENGREP_OWN_LINE = re.compile(r"^[^a-zA-Z0-9]*\s*" + OPENGREP_IGNORE_WORD, re.IGNORECASE)
NOSEMGREP_VALID = re.compile(
    r"^#\s*"
    + LONG_WORD
    + r":\s*[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*\s+[^,\n]*[A-Za-z][^,\n]*$"
)

# Bandit 1.9.4 (NOSEC_COMMENT in bandit/core/manager.py) takes a `#`, optional
# space and lower-case `nosec` anywhere in a comment, even inside a longer word.
NOSEC_DIRECTIVE = re.compile(r"#\s*nosec")
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

# Payloads name the ignore words as {sem}, {sg}, {SG} and {og}; planted() fills them in.
PLANTED_REFUSALS: list[tuple[str, str, str]] = [
    ("bare-ignore-word", "# {sg}\n", "code"),
    ("ignore-word-no-reason", "# {sg}: rule-id\n", "code"),
    ("ignore-word-comma-reason", "# {sg}: rule-id bad, reason\n", "code"),
    ("bare-nosec", "# nosec\n", "code"),
    ("nosec-no-reason", "# nosec B101\n", "code"),
    ("nosec-reason-no-id", "# nosec # reason without id\n", "code"),
    ("short-alias", "# {sem}\n", "code"),
    ("opengrep-alias", "# {og}: rule-id valid test reason\n", "code"),
    ("upper-case-ignore-word", "# {SG}: rule-id valid test reason\n", "code"),
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
    ("ignore-word-one-id", "# {sg}: rule-id valid test reason\n"),
    ("ignore-word-two-ids", "# {sg}: rule-one, rule-two valid test reason\n"),
    ("nosec-two-ids", "# nosec B101, B102 # valid test reason\n"),
    ("split-word-string", 'TEXT = "no" + "semgrep"\n'),
    ("nosec-in-string", 'TEXT = "# nosec"\n'),
    ("own-line-prose", "# This helper explains why {sg} is not used here.\n"),
    ("nosec-prose", "x = 1  # see the nosec docs before adding one\n"),
    ("nosec-upper-case", "x = 1  # NOSEC\n"),
]


def planted(payload: str) -> str:
    """A planted payload with the ignore words filled in."""
    return payload.format(**WORDS)


def comment_problems(comment: str, after_code: bool, where: str) -> list[str]:
    """Read one comment as opengrep and Bandit read it; refuse a suppression without a reason."""
    problems: list[str] = []
    opengrep = OPENGREP_AFTER_CODE if after_code else OPENGREP_OWN_LINE
    if opengrep.search(comment) and not NOSEMGREP_VALID.match(comment):
        problems.append(f"{where}: invalid opengrep suppression: {comment}")
    if NOSEC_DIRECTIVE.search(comment) and not NOSEC_VALID.match(comment):
        problems.append(f"{where}: invalid nosec suppression: {comment}")
    return problems


def token_problems(tok: tokenize.TokenInfo, name: str) -> list[str]:
    """Refuse an opengrep ignore word outside a comment: in a string or a name it suppresses too.

    Bandit reads nosec in comment tokens only, so only the opengrep words count here.
    """
    found = OPENGREP_AFTER_CODE.search(tok.string)
    if not found:
        return []
    line = tok.start[0] + tok.string.count("\n", 0, found.start())
    return [f"{name}:{line}: invalid opengrep ignore word outside a comment: {tok.string.strip()}"]


def suppression_problems(text: str, name: str) -> list[str]:
    """Tokenize text and verify that all suppression comments meet requirements."""
    problems: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                problems.extend(token_problems(tok, name))
                continue
            after_code = bool(tok.line[: tok.start[1]].strip())
            where = f"{name}:{tok.start[0]}"
            problems.extend(comment_problems(tok.string.strip(), after_code, where))
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
