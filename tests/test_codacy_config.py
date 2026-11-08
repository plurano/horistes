# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Checks on .codacy.yml: <path>:<line> references must name the line they describe."""

from __future__ import annotations

import re
from pathlib import Path

from conftest import ROOT

# Any <path>:<line> token counts as a reference, whatever the file type, so a
# reference in a form the checks below do not know fails instead of passing unread.
DEF_REF = re.compile(r"\(([\w./-]+):(\d+)\s+([A-Za-z0-9_]+)\)")
STANDALONE_REF = re.compile(r"([\w./-]+):(\d+)")


def _decide_construct(comment: str) -> str | None:
    """Decide expected construct from comment text using an allow-list."""
    if "CompletedProcess" in comment:
        return "subprocess.CompletedProcess("
    if "import subprocess" in comment:
        return "import subprocess"
    if any(k in comment for k in ("subprocess.run", "dangerous-subprocess-use-audit", "B603")):
        return "subprocess.run("
    return None


def _check_reference(
    rel_path: str,
    lineno: int,
    expected: str | None,
    root: Path,
) -> str | None:
    """Verify that a reference points to a valid file, line, and construct."""
    if expected is None:
        return f"{rel_path}:{lineno}: names no known construct"
    target_file = root / rel_path
    if not target_file.is_file():
        return f"{rel_path}:{lineno}: file does not exist"
    lines = target_file.read_text(encoding="utf-8").splitlines()
    if lineno < 1:
        return f"{rel_path}:{lineno}: line 0 out of range"
    if lineno > len(lines):
        return f"{rel_path}:{lineno}: line past end of file"
    actual_line = lines[lineno - 1]
    if expected not in actual_line:
        return f"{rel_path}:{lineno}: line does not hold {expected!r}"
    return None


def _parse_comments(text: str) -> list[tuple[str, int, str | None]]:
    """Parse YAML comment lines into (path, line, expected_construct) tuples."""
    refs: list[tuple[str, int, str | None]] = []
    current_ref: tuple[str, int] | None = None
    current_text: list[str] = []

    def flush() -> None:
        """Append the current pending reference if any."""
        nonlocal current_ref, current_text
        if current_ref is not None:
            path_str, lineno = current_ref
            refs.append((path_str, lineno, _decide_construct(" ".join(current_text))))
            current_ref = None
            current_text = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("#"):
            flush()
            continue

        comment_line = stripped.lstrip("#").strip()
        for m in DEF_REF.finditer(comment_line):
            refs.append((m.group(1), int(m.group(2)), f"def {m.group(3)}("))

        line_clean = DEF_REF.sub(" ", comment_line)
        match = STANDALONE_REF.search(line_clean)
        if match is not None:
            flush()
            current_ref = (match.group(1), int(match.group(2)))
            current_text = [line_clean]
        elif current_ref is not None:
            current_text.append(line_clean)

    flush()
    return refs


def reference_problems(text: str, root: Path) -> list[str]:
    """Return every problem found in <path>:<line> references within YAML comments."""
    problems = [
        f"{line.strip()}: more than one reference on one line"
        for line in text.splitlines()
        if len(STANDALONE_REF.findall(DEF_REF.sub(" ", line))) > 1
    ]
    for rel_path, lineno, expected in _parse_comments(text):
        problem = _check_reference(rel_path, lineno, expected, root)
        if problem is not None and problem not in problems:
            problems.append(problem)
    return problems


def _tree(tmp_path: Path, source: str, filename: str = "tests/a.py") -> Path:
    """Create a temporary repository root holding source at filename."""
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return tmp_path


def test_committed_codacy_yml_has_no_problems() -> None:
    """The committed .codacy.yml gives no problem against the repository."""
    text = (ROOT / ".codacy.yml").read_text(encoding="utf-8")
    assert reference_problems(text, ROOT) == []


def test_planted_stale_line_gives_one_problem(tmp_path: Path) -> None:
    """Planted: a reference pointing to a line holding something else returns one problem."""
    root = _tree(tmp_path, "import shutil\nimport sys\n", filename="tests/a.py")
    yaml_text = "# tests/a.py:1 B404 import subprocess\n"
    assert reference_problems(yaml_text, root) == [
        "tests/a.py:1: line does not hold 'import subprocess'"
    ]


def test_planted_missing_file_gives_problem(tmp_path: Path) -> None:
    """Planted: a reference naming a file that does not exist returns a problem."""
    root = tmp_path
    yaml_text = "# tests/nonexistent.py:1 B404 import subprocess\n"
    assert reference_problems(yaml_text, root) == ["tests/nonexistent.py:1: file does not exist"]


def test_planted_line_past_end_of_file_gives_problem(tmp_path: Path) -> None:
    """Planted: a reference naming a line past EOF returns a problem."""
    root = _tree(tmp_path, "import subprocess\n", filename="tests/a.py")
    yaml_text = "# tests/a.py:99 B404 import subprocess\n"
    assert reference_problems(yaml_text, root) == ["tests/a.py:99: line past end of file"]


def test_planted_line_0_gives_problem(tmp_path: Path) -> None:
    """Planted: a reference naming line 0 returns a problem."""
    root = _tree(tmp_path, "import subprocess\n", filename="tests/a.py")
    yaml_text = "# tests/a.py:0 B404 import subprocess\n"
    assert reference_problems(yaml_text, root) == ["tests/a.py:0: line 0 out of range"]


def test_planted_comment_naming_no_known_construct_gives_problem(tmp_path: Path) -> None:
    """Planted: a reference whose comment text matches no form returns a problem."""
    root = _tree(tmp_path, "import subprocess\n", filename="tests/a.py")
    yaml_text = "# tests/a.py:1 some unrecognized comment\n"
    assert reference_problems(yaml_text, root) == ["tests/a.py:1: names no known construct"]


def test_boundary_last_line_passes(tmp_path: Path) -> None:
    """Boundary: a reference to the last line of a file passes."""
    root = _tree(tmp_path, "import os\nimport subprocess\n", filename="tests/a.py")
    yaml_text = "# tests/a.py:2 B404 import subprocess\n"
    assert reference_problems(yaml_text, root) == []


def test_planted_two_references_on_one_line_give_problem(tmp_path: Path) -> None:
    """Planted: a second reference on a comment line would go unread, so it fails."""
    root = _tree(tmp_path, "import subprocess\n", filename="tests/a.py")
    yaml_text = "# tests/a.py:1 tests/a.py:1 B404 import subprocess\n"
    assert reference_problems(yaml_text, root) == [
        "# tests/a.py:1 tests/a.py:1 B404 import subprocess: more than one reference on one line"
    ]


def test_planted_reference_to_a_file_of_another_type_is_checked(tmp_path: Path) -> None:
    """Planted: a reference to a file that is no .py file is read like any other."""
    root = _tree(tmp_path, "lint:\n", filename="Makefile")
    yaml_text = "# Makefile:1 B603 subprocess.run\n"
    assert reference_problems(yaml_text, root) == [
        "Makefile:1: line does not hold 'subprocess.run('"
    ]


def test_planted_stale_definition_reference_gives_problem(tmp_path: Path) -> None:
    """Planted: a (<path>:<line> <name>) reference to a line not defining <name> fails."""
    root = _tree(tmp_path, "import os\n\n\ndef external():\n    pass\n", filename="tests/a.py")
    stale = "# runs the command (tests/a.py:1 external) with static flags\n"
    assert reference_problems(stale, root) == ["tests/a.py:1: line does not hold 'def external('"]
    current = "# runs the command (tests/a.py:4 external) with static flags\n"
    assert reference_problems(current, root) == []
