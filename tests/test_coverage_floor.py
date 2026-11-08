# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the test coverage floor and fail_under enforcement."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import ROOT

FLOOR = 94


def test_floor_is_set_and_never_lowered() -> None:
    """Read pyproject.toml and assert tool.coverage.report.fail_under is >= FLOOR."""
    with open(ROOT / "pyproject.toml", "rb") as f:
        config = tomllib.load(f)
    report = config.get("tool", {}).get("coverage", {}).get("report", {})
    assert "fail_under" in report
    assert report["fail_under"] >= FLOOR


# mod.py holds four statements; the test calls first() only, so three run
# and the total is exactly 75%. test_mod.py is left out of the measure.
MOD = "def first() -> int:\n    return 1\n\n\ndef second() -> int:\n    return 2\n"
TEST_MOD = "import mod\n\n\ndef test_one() -> None:\n    assert mod.first() == 1\n"
TOTAL = 75


def _report(tmp_path: Path, fail_under: int) -> subprocess.CompletedProcess[str]:
    """Run coverage over a planted project at 75% and return its report.

    The child env drops every COVERAGE_* key, so a caller's COVERAGE_RCFILE
    or COVERAGE_FILE cannot point the planted run at another config or data
    file.
    """
    (tmp_path / "mod.py").write_text(MOD, encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(TEST_MOD, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nbranch = true\nsource = ["."]\nomit = ["test_mod.py"]\n\n'
        f"[tool.coverage.report]\nfail_under = {fail_under}\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    coverage = [sys.executable, "-m", "coverage"]
    pytest = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "test_mod.py"]
    run = subprocess.run(
        [*coverage, "run", *pytest],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    report = subprocess.run(
        [*coverage, "report"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert f"{TOTAL}%" in report.stdout, report.stdout
    return report


def test_report_fails_below_the_floor(tmp_path: Path) -> None:
    """A 75% total under fail_under = 76 makes coverage report exit 2."""
    assert _report(tmp_path, TOTAL + 1).returncode == 2


def test_report_passes_at_the_floor(tmp_path: Path) -> None:
    """A 75% total under fail_under = 75 passes: equality is not below."""
    assert _report(tmp_path, TOTAL).returncode == 0


def test_report_passes_above_the_floor(tmp_path: Path) -> None:
    """A 75% total under fail_under = 50 passes."""
    assert _report(tmp_path, 50).returncode == 0


def test_caller_coverage_env_does_not_reach_the_planted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller's COVERAGE_RCFILE with fail_under = 0 leaves the 75% run failing."""
    outer = tmp_path / "outer"
    outer.mkdir()
    rcfile = outer / "coveragerc"
    rcfile.write_text("[report]\nfail_under = 0\n", encoding="utf-8")
    monkeypatch.setenv("COVERAGE_RCFILE", str(rcfile))
    project = tmp_path / "project"
    project.mkdir()
    assert _report(project, TOTAL + 1).returncode == 2


def test_gate_reports_without_a_lower_floor() -> None:
    """The Makefile test recipe runs coverage report and no line has --fail-under."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^test:[^\n]*\n((?:\t.*\n)+)", makefile, re.M)
    assert recipe is not None
    assert "coverage report" in recipe[1]
    assert all("--fail-under" not in line for line in makefile.splitlines())
