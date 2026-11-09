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
    ("nosec-hash-expiry", "# nosec B602 # expires 2026-11-12 category B: valid test reason\n"),
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


LONG_NOSEMGREP_E501 = (
    f"# {exceptions.LONG_WORD}: dangerous-subprocess-use-audit expires 2026-11-12 "
    "category B: long reason that easily exceeds one hundred columns  # noqa: E501\n"
)


def test_trailing_noqa_e501_positive() -> None:
    """Positive: own-line fleet rule over 100 cols with trailing noqa E501 and entry passes."""
    today = datetime.date(2026, 11, 9)
    valid = {("E501", "test.py")}
    probs = suppression_problems(
        LONG_NOSEMGREP_E501, "test.py", today=today, valid_exceptions=valid
    )
    assert probs == []


def test_trailing_noqa_e501_negative_without_entry() -> None:
    """Negative: same line without register entry raises invalid noqa and exceeds 100 columns."""
    today = datetime.date(2026, 11, 9)
    probs = suppression_problems(
        LONG_NOSEMGREP_E501, "test.py", today=today, valid_exceptions=set()
    )
    assert len(probs) == 2
    assert "invalid noqa suppression: # noqa: E501" in probs[0]
    assert f"{exceptions.LONG_WORD} line exceeds 100 columns" in probs[1]


@pytest.mark.parametrize(
    ("payload", "valid", "expected_probs"),
    [
        (
            LONG_NOSEMGREP_E501,
            {("E501", "other.py")},
            [
                "test.py:1: invalid noqa suppression: # noqa: E501",
                f"test.py:1: {exceptions.LONG_WORD} line exceeds 100 columns",
            ],
        ),
        (
            LONG_NOSEMGREP_E501.replace("E501", "E731"),
            {("E501", "test.py")},
            [
                "test.py:1: invalid noqa suppression: # noqa: E731",
                f"test.py:1: {exceptions.LONG_WORD} line exceeds 100 columns",
            ],
        ),
        (
            "# noqa: E501\n",
            set(),
            ["test.py:1: invalid noqa suppression: # noqa: E501"],
        ),
    ],
    ids=["entry-other-path", "trailing-e731", "standalone-noqa"],
)
def test_trailing_noqa_e501_boundaries(
    payload: str,
    valid: set[tuple[str, str]],
    expected_probs: list[str],
) -> None:
    """Boundary conditions: wrong path, wrong code and standalone noqa are refused."""
    today = datetime.date(2026, 11, 9)
    probs = suppression_problems(payload, "test.py", today=today, valid_exceptions=valid)
    assert probs == expected_probs


@pytest.mark.parametrize(
    ("content", "expected_problems"),
    [
        (
            "engines:\n  opengrep:\n    exclude_paths:\n      - 'tests/a.py'\n",
            ["engines.opengrep.exclude_paths is not permitted"],
        ),
        ("not: valid: yaml: [", []),
        ("- list\n- item\n", []),
        ("just a plain string\n", []),
        ("engines: 'not-a-dict'\n", []),
        (
            "engines:\n  tool1: 'not-a-dict'\n  tool2:\n    enabled: true\n",
            [],
        ),
        (
            "engines:\n  tool1: 'not-a-dict'\n  tool2:\n    exclude_paths:\n      - 'a.py'\n",
            ["engines.tool2.exclude_paths is not permitted"],
        ),
    ],
    ids=[
        "permitted-check-failure",
        "invalid-yaml-syntax",
        "doc-is-list",
        "doc-is-string",
        "engines-not-dict",
        "engine-configs-without-exclude",
        "mixed-engine-configs",
    ],
)
def test_codacy_problems_branches(content: str, expected_problems: list[str]) -> None:
    """codacy_problems handles invalid YAML, non-dict roots, and non-dict engine configs."""
    assert codacy_problems(content) == expected_problems


@pytest.mark.parametrize(
    ("comment", "valid_exceptions", "expected_probs", "expected_count"),
    [
        (
            "# " + "type: ignore[arg-type] # expires 2026-11-12 category B: valid reason",
            set(),
            [],
            1,
        ),
        (
            "# " + "type: ignore[arg-type]",
            set(),
            ["test.py:1: invalid type: ignore suppression: # type: ignore[arg-type]"],
            1,
        ),
        (
            "# " + "type: ignore[arg-type]",
            {("arg-type", "test.py")},
            [],
            1,
        ),
        (
            "# " + "type: ignore[arg-type, name-defined]",
            set(),
            ["test.py:1: invalid type: ignore suppression: # type: ignore[arg-type, name-defined]"],
            1,
        ),
        (
            "# " + "type: ignore # expires 2026-11-12 category B: valid reason",
            set(),
            [],
            1,
        ),
        (
            "# " + "type: ignore[arg-type]",
            set(),
            ["test.py:1: invalid type: ignore suppression: # type: ignore[arg-type]"],
            None,
        ),
    ],
    ids=[
        "fleet-with-rule-code",
        "plain-uncovered",
        "plain-covered-by-exception",
        "plain-uncovered-multi",
        "fleet-bare-directive",
        "plain-without-counts",
    ],
)
def test_type_ignore_directive_lifecycle(
    comment: str,
    valid_exceptions: set[tuple[str, str]],
    expected_probs: list[str],
    expected_count: int | None,
) -> None:
    """type: ignore directives support fleet format, plain format with exceptions, and counts."""
    counts: dict[str, int] | None = None if expected_count is None else {}
    probs = exceptions.comment_problems(
        comment,
        after_code=True,
        where="test.py:1",
        today=datetime.date(2026, 11, 9),
        valid_exceptions=valid_exceptions,
        path="test.py",
        counts=counts,
    )
    assert probs == expected_probs
    if counts is not None:
        assert counts.get("type: ignore") == expected_count


@pytest.mark.parametrize(
    ("comment", "valid_exceptions", "expected_probs", "expected_count"),
    [
        (
            "# " + "pylint: disable=broad-except expires 2026-11-12 category B: valid reason",
            set(),
            [],
            1,
        ),
        (
            "# " + "pylint: disable=broad-except",
            set(),
            ["test.py:1: invalid pylint suppression: # pylint: disable=broad-except"],
            1,
        ),
        (
            "# " + "pylint: disable=broad-except",
            {("broad-except", "test.py")},
            [],
            1,
        ),
        (
            "# " + "pylint: disable-next=broad-except expires 2026-11-12 category B: valid reason",
            set(),
            [],
            1,
        ),
        (
            "# " + "pylint: disable=broad-except,no-member",
            {("broad-except", "test.py"), ("no-member", "test.py")},
            [],
            1,
        ),
        (
            "# " + "pylint: disable=broad-except",
            set(),
            ["test.py:1: invalid pylint suppression: # pylint: disable=broad-except"],
            None,
        ),
    ],
    ids=[
        "fleet-valid",
        "plain-uncovered",
        "plain-covered-by-exception",
        "disable-next-fleet",
        "multiple-rules-covered",
        "plain-without-counts",
    ],
)
def test_pylint_directive_lifecycle(
    comment: str,
    valid_exceptions: set[tuple[str, str]],
    expected_probs: list[str],
    expected_count: int | None,
) -> None:
    """pylint directives support fleet format, plain format with exceptions, and counts."""
    counts: dict[str, int] | None = None if expected_count is None else {}
    probs = exceptions.comment_problems(
        comment,
        after_code=False,
        where="test.py:1",
        today=datetime.date(2026, 11, 9),
        valid_exceptions=valid_exceptions,
        path="test.py",
        counts=counts,
    )
    assert probs == expected_probs
    if counts is not None:
        assert counts.get("pylint") == expected_count


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("# ordinary comment with no noqa", False),
        ("# noqa: E501", True),
        ("# noqa: E501, W291", True),
        ("# noqa: W291, E501", True),
        ("# noqa: W291", False),
        ("# noqa: E501 (not trailing)", False),
        ("", False),
    ],
    ids=[
        "no-noqa-at-all",
        "exact-e501",
        "e501-first",
        "e501-second",
        "different-code",
        "noqa-not-trailing",
        "empty-comment",
    ],
)
def test_has_trailing_noqa_e501_cases(comment: str, expected: bool) -> None:
    """_has_trailing_noqa_e501 detects trailing E501 and refuses comments without noqa."""
    assert exceptions._has_trailing_noqa_e501(comment) is expected


@pytest.mark.parametrize(
    ("text", "name", "expected_err_part"),
    [
        ("(\n", "unclosed_paren.py", "EOF in multi-line statement"),
        ('"""unclosed docstring', "unclosed_doc.py", "EOF in multi-line string"),
        ("x = 1\n", "valid.py", ""),
    ],
    ids=["unclosed-parenthesis", "unclosed-docstring", "valid-syntax"],
)
def test_suppression_problems_tokenize_error(text: str, name: str, expected_err_part: str) -> None:
    """suppression_problems catches TokenError on invalid Python syntax and reports line 1."""
    probs = suppression_problems(text, name)
    if expected_err_part:
        assert len(probs) == 1
        assert f"{name}:1: tokenize error: " in probs[0]
        assert expected_err_part in probs[0]
    else:
        assert probs == []
