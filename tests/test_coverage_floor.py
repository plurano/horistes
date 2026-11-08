# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the test coverage floor and how the gate applies it."""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from pathlib import Path

import coverage
import coverage.cmdline
import pytest
from conftest import ROOT

FLOOR = 94
RCFILE = "--rcfile=pyproject.toml"
COVERAGE = ["$(BIN)/python", "-B", "-m", "coverage"]
# The Makefile test recipe runs these coverage commands in this order, each
# reading pyproject.toml only. Environment assignments may come first.
GATE_COVERAGE = [
    [*COVERAGE, "erase", RCFILE],
    [*COVERAGE, "run", RCFILE, "-m", "pytest"],
    [*COVERAGE, "combine", RCFILE],
    [*COVERAGE, "report", RCFILE],
]
ASSIGNMENT = re.compile(r"[A-Z][A-Z0-9_]*=")


def test_floor_is_set_and_never_lowered() -> None:
    """Read pyproject.toml and assert tool.coverage.report.fail_under is >= FLOOR."""
    with open(ROOT / "pyproject.toml", "rb") as f:
        config = tomllib.load(f)
    report = config.get("tool", {}).get("coverage", {}).get("report", {})
    assert "fail_under" in report
    assert report["fail_under"] >= FLOOR


# mod.py holds four statements; lines 1, 2, and 5 are marked measured,
# so three of four run and the total is exactly 75%.
MOD = "def first() -> int:\n    return 1\n\n\ndef second() -> int:\n    return 2\n"
MEASURED = {1, 2, 5}
EVERY_LINE = {1, 2, 5, 6}
TOTAL = 75


def _drop_coverage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every COVERAGE_* variable, so a caller's settings stay out."""
    for key in list(os.environ):
        if key.startswith("COVERAGE_"):
            monkeypatch.delenv(key)


def _plant(project: Path, fail_under: int, run: str = 'source = ["."]') -> None:
    """Write mod.py and a pyproject.toml holding the given floor under project."""
    (project / "mod.py").write_text(MOD, encoding="utf-8")
    (project / "pyproject.toml").write_text(
        f"[tool.coverage.run]\n{run}\n\n[tool.coverage.report]\nfail_under = {fail_under}\n",
        encoding="utf-8",
    )


def _shard(project: Path, lines: set[int], suffix: str | None = None) -> None:
    """Write a coverage data file marking the given lines of mod.py as run."""
    data = coverage.CoverageData(basename=str(project / ".coverage"), suffix=suffix)
    data.add_lines({str(project / "mod.py"): lines})
    data.write()


def _coverage(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the coverage command line in this process; return status and output."""
    status = coverage.cmdline.main(argv)
    return status or 0, capsys.readouterr().out


def _report(
    tmp_path: Path,
    fail_under: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> int:
    """Report coverage over a planted project at 75% and return its status.

    Environment variables starting with COVERAGE_* are removed so a
    caller's COVERAGE_RCFILE or COVERAGE_FILE cannot point the run at
    another config or data file.
    """
    _plant(tmp_path, fail_under)
    monkeypatch.chdir(tmp_path)
    _drop_coverage_env(monkeypatch)
    _shard(tmp_path, MEASURED)
    status, out = _coverage(["report"], capsys)
    assert f"{TOTAL}%" in out, out
    return status


def test_report_fails_below_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 75% total under fail_under = 76 makes coverage report exit 2."""
    assert _report(tmp_path, TOTAL + 1, monkeypatch, capsys) == 2


def test_report_passes_at_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 75% total under fail_under = 75 passes: equality is not below."""
    assert _report(tmp_path, TOTAL, monkeypatch, capsys) == 0


def test_report_passes_above_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 75% total under fail_under = 50 passes."""
    assert _report(tmp_path, 50, monkeypatch, capsys) == 0


def test_caller_coverage_env_does_not_reach_the_planted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A caller's COVERAGE_RCFILE with fail_under = 0 leaves the 75% run failing."""
    outer = tmp_path / "outer"
    outer.mkdir()
    rcfile = outer / "coveragerc"
    rcfile.write_text("[report]\nfail_under = 0\n", encoding="utf-8")
    monkeypatch.setenv("COVERAGE_RCFILE", str(rcfile))
    project = tmp_path / "project"
    project.mkdir()
    assert _report(project, TOTAL + 1, monkeypatch, capsys) == 2


def test_rcfile_flag_keeps_a_caller_rcfile_from_lowering_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With COVERAGE_RCFILE at fail_under = 0, report passes and report --rcfile fails."""
    rcfile = tmp_path / "coveragerc"
    rcfile.write_text("[report]\nfail_under = 0\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    _plant(project, TOTAL + 1)
    monkeypatch.chdir(project)
    _drop_coverage_env(monkeypatch)
    monkeypatch.setenv("COVERAGE_RCFILE", str(rcfile))
    _shard(project, MEASURED)
    assert _coverage(["report"], capsys)[0] == 0
    status, out = _coverage(["report", RCFILE], capsys)
    assert f"{TOTAL}%" in out, out
    assert status == 2


def _combined(
    project: Path,
    erase: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    """Leave a stale 100% shard, run a fresh 75% one, combine and report.

    The planted config carries the repository's patch = ["subprocess"],
    which turns on parallel mode, so erase also deletes .coverage.* shards.
    """
    project.mkdir()
    _plant(project, TOTAL + 1, 'source = ["."]\npatch = ["subprocess"]')
    monkeypatch.chdir(project)
    _drop_coverage_env(monkeypatch)
    _shard(project, EVERY_LINE, "stale")
    if erase:
        assert _coverage(["erase", RCFILE], capsys)[0] == 0
        assert not list(project.glob(".coverage*"))
    _shard(project, MEASURED, "fresh")
    assert _coverage(["combine", RCFILE], capsys)[0] == 0
    return _coverage(["report", RCFILE], capsys)


def test_stale_shard_lifts_the_total_without_erase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without erase, combine takes the stale shard and the 75% run passes at 100%."""
    status, out = _combined(tmp_path / "kept", False, monkeypatch, capsys)
    assert "100%" in out, out
    assert status == 0


def test_erase_drops_a_stale_shard_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Erase first, and the same 75% run fails under fail_under = 76."""
    status, out = _combined(tmp_path / "erased", True, monkeypatch, capsys)
    assert f"{TOTAL}%" in out, out
    assert status == 2


def _recipe_commands(makefile: str, target: str) -> list[list[str]]:
    """Return the target's recipe as one token list per logical shell line."""
    recipe = re.search(rf"^{target}:[^\n]*\n((?:\t.*\n)+)", makefile, re.M)
    assert recipe is not None, target
    joined = recipe[1].replace("\\\n", " ")
    return [shlex.split(line) for line in joined.splitlines() if line.strip()]


def test_gate_runs_each_coverage_step_from_pyproject() -> None:
    """The test recipe runs erase, run, combine and report as written, nothing else.

    Each step starts the pinned coverage with --rcfile=pyproject.toml, so an
    inherited COVERAGE_RCFILE or a stray .coveragerc cannot set the floor;
    a step behind echo, with || true or with --fail-under fails here.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    steps = []
    for tokens in _recipe_commands(makefile, "test"):
        start = next(
            (i for i, token in enumerate(tokens) if not ASSIGNMENT.match(token)), len(tokens)
        )
        if any("coverage" in token for token in tokens):
            steps.append(tokens[start:])
    assert steps == GATE_COVERAGE
    assert all("--fail-under" not in line for line in makefile.splitlines())
