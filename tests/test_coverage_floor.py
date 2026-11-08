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
DATA_FILE = '--data-file="$$data/.coverage"'
COVERAGE = ["$(BIN)/python", "-B", "-m", "coverage"]
# The suite runs in Python's development mode (tests/test_dev_mode.py).
DEV_COVERAGE = ["$(BIN)/python", "-X", "dev", "-B", "-m", "coverage"]
STRICT = ["--precision=10", "--format=total"]
# The Makefile test recipe runs these coverage commands in this order, each
# reading pyproject.toml only and writing data files to $$data. The run step
# starts Python in development mode; the last step compares the unrounded
# total with the floor at ten decimals.
# Environment assignments may come first.
GATE_COVERAGE = [
    [*DEV_COVERAGE, "run", RCFILE, DATA_FILE, "-m", "pytest"],
    [*COVERAGE, "combine", RCFILE, DATA_FILE],
    [*COVERAGE, "report", RCFILE, DATA_FILE],
    [*COVERAGE, "report", RCFILE, DATA_FILE, *STRICT],
]
ASSIGNMENT = re.compile(r"[A-Z][A-Z0-9_]*=")


def test_floor_is_set_and_never_lowered() -> None:
    """Read pyproject.toml and assert tool.coverage.report.fail_under is >= FLOOR."""
    with open(ROOT / "pyproject.toml", "rb") as f:
        config = tomllib.load(f)
    report = config.get("tool", {}).get("coverage", {}).get("report", {})
    assert "fail_under" in report
    assert report["fail_under"] >= FLOOR


def test_precision_is_set_to_at_least_two_decimals() -> None:
    """Read pyproject.toml and assert tool.coverage.report.precision is >= 2."""
    with open(ROOT / "pyproject.toml", "rb") as f:
        config = tomllib.load(f)
    report = config.get("tool", {}).get("coverage", {}).get("report", {})
    assert "precision" in report
    assert report["precision"] >= 2


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


def _plant(
    project: Path,
    fail_under: int,
    run: str = 'source = ["."]',
    report: str = "",
) -> None:
    """Write mod.py and a pyproject.toml holding the given floor under project."""
    (project / "mod.py").write_text(MOD, encoding="utf-8")
    report_extra = f"\n{report}" if report else ""
    content = (
        f"[tool.coverage.run]\n{run}\n\n"
        f"[tool.coverage.report]\nfail_under = {fail_under}{report_extra}\n"
    )
    (project / "pyproject.toml").write_text(content, encoding="utf-8")


def _shard(
    project: Path,
    lines: set[int],
    suffix: str | None = None,
    data_dir: Path | None = None,
) -> None:
    """Write a coverage data file marking the given lines of mod.py as run."""
    base = (data_dir or project) / ".coverage"
    data = coverage.CoverageData(basename=str(base), suffix=suffix)
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
    """The _report helper removes a caller's COVERAGE_* variables before the planted run.

    test_rcfile_flag_keeps_a_caller_rcfile_from_lowering_the_floor holds the
    gate's --rcfile against a variable set after that removal.
    """
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


def test_precision_prevents_rounding_up_to_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 75.76% total rounds up to 76% without precision, but precision = 2 fails under 76."""
    code = "\n".join(f"x{i} = {i}" for i in range(1, 34)) + "\n"
    (tmp_path / "mod.py").write_text(code, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _drop_coverage_env(monkeypatch)
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({str(tmp_path / "mod.py"): set(range(1, 26))})
    data.write()

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.coverage.run]\nsource = ["."]\n\n[tool.coverage.report]\nfail_under = 76\n',
        encoding="utf-8",
    )
    status_no_prec, out_no_prec = _coverage(["report"], capsys)
    assert status_no_prec == 0
    assert "76%" in out_no_prec

    pyproject.write_text(
        '[tool.coverage.run]\nsource = ["."]\n\n'
        "[tool.coverage.report]\nfail_under = 76\nprecision = 2\n",
        encoding="utf-8",
    )
    status_prec, out_prec = _coverage(["report"], capsys)
    assert status_prec == 2
    assert "75.76%" in out_prec


def _plant_statements(
    project: Path, statements: int, run: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plant mod.py with the given statements, the first run of them measured.

    The planted pyproject.toml holds fail_under = FLOOR and precision = 2,
    as the repository's does; the working directory moves to project and
    every COVERAGE_* variable is removed.
    """
    code = "".join(f"x{i} = {i}\n" for i in range(1, statements + 1))
    (project / "mod.py").write_text(code, encoding="utf-8")
    pyproject = (
        '[tool.coverage.run]\nsource = ["."]\n\n'
        f"[tool.coverage.report]\nfail_under = {FLOOR}\nprecision = 2\n"
    )
    (project / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    monkeypatch.chdir(project)
    _drop_coverage_env(monkeypatch)
    data = coverage.CoverageData(basename=str(project / ".coverage"))
    data.add_lines({str(project / "mod.py"): set(range(1, run + 1))})
    data.write()


def test_strict_step_fails_a_total_that_rounds_up_to_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """407 of 433 (93.995%) passes the two-decimal report as 94.00% and fails the strict step."""
    _plant_statements(tmp_path, 433, 407, monkeypatch)

    status_prec, out_prec = _coverage(["report", RCFILE], capsys)
    assert status_prec == 0
    assert "94.00%" in out_prec

    status_strict, out_strict = _coverage(["report", RCFILE, *STRICT], capsys)
    assert status_strict == 2
    assert "93.99538" in out_strict


def test_strict_step_passes_a_total_at_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """47 of 50 is exactly 94%, so the strict step passes: equality is not below."""
    _plant_statements(tmp_path, 50, 47, monkeypatch)

    status, out = _coverage(["report", RCFILE, *STRICT], capsys)
    assert status == 0
    assert out.strip() == "94.0000000000", out


def _combined(
    project: Path,
    data_dir: Path | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    """Leave a stale 100% shard, run a fresh 75% one, combine and report.

    The planted config carries the repository's patch = ["subprocess"],
    which turns on parallel mode.
    """
    project.mkdir()
    _plant(project, TOTAL + 1, 'source = ["."]\npatch = ["subprocess"]')
    monkeypatch.chdir(project)
    _drop_coverage_env(monkeypatch)
    _shard(project, EVERY_LINE, "stale")
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        data_arg = f"--data-file={data_dir / '.coverage'}"
        _shard(project, MEASURED, "fresh", data_dir=data_dir)
        assert _coverage(["combine", RCFILE, data_arg], capsys)[0] == 0
        return _coverage(["report", RCFILE, data_arg], capsys)
    _shard(project, MEASURED, "fresh")
    assert _coverage(["combine", RCFILE], capsys)[0] == 0
    return _coverage(["report", RCFILE], capsys)


def test_stale_shard_lifts_the_total_without_erase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a separate data directory, combine takes the stale shard and passes at 100%."""
    status, out = _combined(tmp_path / "kept", None, monkeypatch, capsys)
    assert "100%" in out, out
    assert status == 0


def test_data_file_in_new_directory_ignores_stale_root_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --data-file in a new directory, a stale root shard is ignored and 75% fails under 76."""
    project = tmp_path / "isolated"
    status, out = _combined(project, tmp_path / "data", monkeypatch, capsys)
    assert f"{TOTAL}%" in out, out
    assert status == 2
    assert (project / ".coverage.stale").exists()


def _recipe_lines(makefile: str, target: str) -> list[str]:
    """Return the target's recipe as one string per command, split at &&."""
    recipe = re.search(rf"^{target}:[^\n]*\n((?:\t.*\n)+)", makefile, re.M)
    assert recipe is not None, target
    joined = recipe[1].replace("\\\n", " ")
    commands = [cmd.strip() for line in joined.splitlines() for cmd in line.split("&&")]
    return [cmd for cmd in commands if cmd]


def _recipe_commands(makefile: str, target: str) -> list[list[str]]:
    """Return the target's recipe as one token list per command, quotes kept."""
    return [shlex.split(cmd, posix=False) for cmd in _recipe_lines(makefile, target)]


def test_gate_runs_each_coverage_step_from_pyproject() -> None:
    """The test recipe runs run, combine, report and strict report in $$data, nothing else.

    Each step starts the pinned coverage with --rcfile=pyproject.toml and
    the same --data-file="$$data/.coverage", so runs cannot collide or use
    stale data; a step behind echo, with || true or with --fail-under fails here.
    A trap set right after mktemp removes $$data when the recipe exits.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    commands = _recipe_lines(makefile, "test")
    first = next((i for i, cmd in enumerate(commands) if "coverage" in cmd), len(commands))
    setup = commands[:first]
    made = next((i for i, cmd in enumerate(setup) if re.search(r"\bdata=.*mktemp -d\b", cmd)), None)
    assert made is not None, setup
    assert setup[made + 1 : made + 2] == ["trap 'rm -rf \"$$data\"' EXIT"], setup
    steps = []
    for tokens in _recipe_commands(makefile, "test"):
        start = next(
            (i for i, token in enumerate(tokens) if not ASSIGNMENT.match(token)), len(tokens)
        )
        if any("coverage" in token for token in tokens):
            steps.append(tokens[start:])
    assert steps == GATE_COVERAGE
    assert all("--fail-under" not in line for line in makefile.splitlines())


def test_coverage_install_is_hash_pinned() -> None:
    """Read requirements/tools.txt and Makefile to assert coverage is hash-pinned."""
    tools_txt = (ROOT / "requirements/tools.txt").read_text(encoding="utf-8").splitlines()
    cov_idx = next(
        (i for i, line in enumerate(tools_txt) if line.startswith("coverage==")),
        None,
    )
    assert cov_idx is not None, "coverage== requirement not found in requirements/tools.txt"

    hashes = []
    for line in tools_txt[cov_idx + 1 :]:
        stripped = line.strip()
        if stripped.startswith("--hash=sha256:"):
            hashes.append(stripped)
        elif stripped and not stripped.startswith("#") and not stripped.startswith("\\"):
            break

    assert len(hashes) >= 2, f"expected at least two --hash=sha256: lines, got {len(hashes)}"

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    tools_recipe = _recipe_lines(makefile, "tools")
    assert any(
        "--require-hashes" in line and "-r requirements/tools.txt" in line for line in tools_recipe
    ), tools_recipe
