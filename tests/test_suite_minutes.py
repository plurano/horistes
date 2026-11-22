# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for tools/suite_minutes.py and Makefile timing pins."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import ROOT, Tree
from test_coverage_floor import _recipe_lines
from test_dev_mode import _child

sys.path.insert(0, str(ROOT / "tools"))
import suite_minutes

BUDGET = 900


def _write_junit(
    path: Path,
    *,
    wall: str,
    cases: list[tuple[str, str, str]],
    bare_root: bool = False,
) -> None:
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    suite_open = f'<testsuite name="pytest" time="{wall}">'
    case_lines = [
        f'<testcase classname="{cls}" name="{name}" time="{t}"/>' for cls, name, t in cases
    ]
    if bare_root:
        lines.append(suite_open)
        lines.extend(case_lines)
        lines.append("</testsuite>")
    else:
        lines.append("<testsuites>")
        lines.append(suite_open)
        lines.extend(case_lines)
        lines.append("</testsuite>")
        lines.append("</testsuites>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_tool(tree: Tree, report: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return tree.tool("suite_minutes.py", "check", str(report.resolve()), *args)


def test_within_budget_passes(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    cases = [(f"mod{i}", f"t{i}", f"{i * 0.1:.1f}") for i in range(5)]
    _write_junit(report, wall="5.0", cases=cases)
    done = run_tool(tree, report, "--budget-seconds", "100")
    assert done.returncode == 0, done.stderr
    assert "PASS: test stage wall time 5.00s within the 100s budget (5%), 5 tests" in done.stdout
    assert done.stdout.splitlines()[0] == "slowest 5 of 5 tests:"
    assert "  0.40s mod4::t4" in done.stdout
    assert done.stderr == ""


def test_slowest_two_of_five(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(
        report,
        wall="3.0",
        cases=[
            ("a", "one", "1.0"),
            ("b", "two", "3.0"),
            ("c", "three", "2.0"),
            ("d", "four", "0.5"),
            ("e", "five", "2.0"),
        ],
    )
    done = run_tool(tree, report, "--budget-seconds", "10", "--slowest", "2")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "slowest 2 of 5 tests:"
    assert lines[1] == "  3.00s b::two"
    assert lines[2] == "  2.00s c::three"


def test_default_slowest_shows_ten_of_twelve(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    cases = [(f"c{i}", f"n{i}", "0.1") for i in range(12)]
    _write_junit(report, wall="2.0", cases=cases)
    done = run_tool(tree, report, "--budget-seconds", "10")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "slowest 10 of 12 tests:"


def test_bare_testsuite_root(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1.5", cases=[("x", "y", "0.2")], bare_root=True)
    done = run_tool(tree, report, "--budget-seconds", "5")
    assert done.returncode == 0, done.stderr


def test_wall_time_not_sum_of_cases(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(
        report,
        wall="100",
        cases=[("a", "t1", "100"), ("b", "t2", "100"), ("c", "t3", "100")],
    )
    done = run_tool(tree, report, "--budget-seconds", "200")
    assert done.returncode == 0, done.stderr


def test_real_pytest_junit_report(tree: Tree, tmp_path: Path) -> None:
    test_file = tmp_path / "test_two.py"
    test_file.write_text(
        textwrap.dedent(
            """\
            import time

            def test_fast():
                time.sleep(0.01)

            def test_slow():
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    junit = tmp_path / "r.xml"
    env = {**os.environ, "PYTEST_ADDOPTS": ""}
    pytest_run = _child(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            str(test_file),
            f"--junitxml={junit}",
        ],
        cwd=tree.root,
        env=env,
    )
    assert pytest_run.returncode == 0, pytest_run.stdout + pytest_run.stderr
    done = run_tool(tree, junit, "--budget-seconds", "60")
    assert done.returncode == 0, done.stderr
    assert "test_two::test_fast" in done.stdout
    assert "test_two::test_slow" in done.stdout


def test_over_budget_fails(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="50", cases=[("a", "t", "1")])
    done = run_tool(tree, report, "--budget-seconds", "10")
    assert done.returncode == 1, done.stdout
    assert "FAIL: test stage wall time 50.00s exceeds the 10s budget (500%), 1 tests" in done.stderr
    assert done.stdout.splitlines()[0] == "slowest 1 of 1 tests:"


def test_read_report_unreadable_parse_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[])
    err = PermissionError(13, "Permission denied")

    def _deny_parse(_path: Path) -> object:
        raise err

    monkeypatch.setattr(suite_minutes.DefusedET, "parse", _deny_parse)
    with pytest.raises(ValueError, match="could not read") as caught:
        suite_minutes.read_report(report)
    assert "Permission denied" in str(caught.value)


def test_read_report_stat_error_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stat error from Path.is_file (raised on Python 3.12, the CI version) is a read reason."""
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[])

    def _deny_stat(_self: Path, **_kwargs: object) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(suite_minutes.Path, "is_file", _deny_stat)
    with pytest.raises(ValueError, match="could not read .*: Permission denied"):
        suite_minutes.read_report(report)


def test_check_unreadable_report_exit_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[])

    def _deny_parse(_path: Path) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(suite_minutes.DefusedET, "parse", _deny_parse)
    code = suite_minutes.main(["check", str(report), "--budget-seconds", "10"])
    captured = capsys.readouterr()
    assert code == 3
    assert "ERROR: could not measure: could not read" in captured.err
    assert captured.out == ""


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores chmod permissions",
)
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows ignores chmod 000 permissions",
)
def test_unreadable_report_file_exit_three(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[])
    report.chmod(0o000)
    try:
        done = run_tool(tree, report, "--budget-seconds", "10")
        assert done.returncode == 3
        assert "could not read" in done.stderr
        assert done.stdout == ""
    finally:
        report.chmod(0o644)


@pytest.mark.parametrize(
    ("factory", "reason"),
    [
        pytest.param(lambda p: None, "no report at", id="missing"),
        pytest.param(
            lambda p: p.write_text("not xml", encoding="utf-8"),
            "not valid XML",
            id="bad-xml",
        ),
        pytest.param(
            lambda p: p.write_text("<testsuites></testsuites>", encoding="utf-8"),
            "expected one testsuite, found 0",
            id="zero-suites",
        ),
        pytest.param(
            lambda p: p.write_text(
                "<testsuites><testsuite time='1'/><testsuite time='1'/></testsuites>",
                encoding="utf-8",
            ),
            "expected one testsuite, found 2",
            id="two-suites",
        ),
        pytest.param(
            lambda p: p.write_text(
                "<!DOCTYPE t [<!ENTITY a 'b'>]><testsuite time='1'>&a;</testsuite>",
                encoding="utf-8",
            ),
            "is not valid XML: EntitiesForbidden",
            id="entity-declaration",
        ),
    ],
)
def test_measure_errors_exit_three(tree: Tree, tmp_path: Path, factory, reason: str) -> None:
    report = tmp_path / "r.xml"
    factory(report)
    done = run_tool(tree, report, "--budget-seconds", "10")
    assert done.returncode == 3
    assert "ERROR: could not measure:" in done.stderr
    assert reason in done.stderr
    assert done.stdout == ""


def test_invalid_suite_time_exit_three(tree: Tree, tmp_path: Path) -> None:
    for wall in ("", "abc", "-1", "nan", "inf"):
        report = tmp_path / f"suite-{wall or 'missing'}.xml"
        if wall:
            _write_junit(report, wall=wall, cases=[])
        else:
            report.write_text(
                '<?xml version="1.0"?><testsuite><testcase name="t" time="1"/></testsuite>',
                encoding="utf-8",
            )
        done = run_tool(tree, report, "--budget-seconds", "10")
        assert done.returncode == 3, wall
        assert "ERROR: could not measure:" in done.stderr
        assert done.stdout == ""


def test_invalid_testcase_time_exit_three(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[("c", "n", "abc")])
    done = run_tool(tree, report, "--budget-seconds", "10")
    assert done.returncode == 3
    assert "test case c::n time is not a finite number >= 0" in done.stderr


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "nan"])
def test_invalid_budget_exit_two(tree: Tree, tmp_path: Path, bad: str) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[])
    done = run_tool(tree, report, "--budget-seconds", bad)
    assert done.returncode == 2


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["check", "REPORT"], id="no-budget"),
        pytest.param(["measure", "REPORT", "--budget-seconds", "10"], id="unknown-command"),
        pytest.param(["--budget-seconds", "10"], id="no-command"),
    ],
)
def test_usage_errors_exit_two(tree: Tree, tmp_path: Path, argv: list[str]) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[])
    args = [str(report.resolve()) if arg == "REPORT" else arg for arg in argv]
    done = tree.tool("suite_minutes.py", *args)
    assert done.returncode == 2, done.stdout + done.stderr
    assert done.stdout == ""


def test_tools_in_lists_defusedxml() -> None:
    """The report reader's XML library is in the tool pins the gate installs."""
    text = (ROOT / "requirements" / "tools.in").read_text(encoding="utf-8")
    assert "defusedxml" in text.split()
    pinned = (ROOT / "requirements" / "tools.txt").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("defusedxml==") for line in pinned)


@pytest.mark.parametrize("bad", ["-1", "101"])
def test_invalid_slowest_exit_two(tree: Tree, tmp_path: Path, bad: str) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[])
    done = run_tool(tree, report, "--budget-seconds", "10", "--slowest", bad)
    assert done.returncode == 2


def test_wall_equal_budget_passes(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="10", cases=[])
    done = run_tool(tree, report, "--budget-seconds", "10")
    assert done.returncode == 0


def test_wall_over_budget_by_epsilon_fails(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="10.01", cases=[])
    done = run_tool(tree, report, "--budget-seconds", "10")
    assert done.returncode == 1


def test_slowest_zero_prints_nothing(tree: Tree, tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    _write_junit(report, wall="1", cases=[("a", "b", "0.5")])
    done = run_tool(tree, report, "--budget-seconds", "10", "--slowest", "0")
    assert done.returncode == 0
    assert not any(line.startswith("slowest") for line in done.stdout.splitlines())


def test_read_report_max_tests_boundary(tmp_path: Path) -> None:
    four = tmp_path / "four.xml"
    _write_junit(
        four,
        wall="1",
        cases=[(f"c{i}", f"n{i}", "0.1") for i in range(4)],
    )
    with pytest.raises(ValueError, match="more than 3 test cases"):
        suite_minutes.read_report(four, max_tests=3)
    three = tmp_path / "three.xml"
    _write_junit(
        three,
        wall="1",
        cases=[(f"c{i}", f"n{i}", "0.1") for i in range(3)],
    )
    got = suite_minutes.read_report(three, max_tests=3)
    assert len(got.tests) == 3


def test_slowest_tie_breaks_by_id() -> None:
    tests = [("b::t", 1.0), ("a::t", 1.0)]
    assert suite_minutes.slowest(tests, 2) == [("a::t", 1.0), ("b::t", 1.0)]


def test_makefile_test_budget_seconds_is_pinned() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^TEST_BUDGET_SECONDS := (\d+)\s*$", makefile, re.M)
    assert match is not None
    value = int(match.group(1))
    assert 1 <= value <= BUDGET == 900


def test_makefile_test_recipe_junit_and_timing_check() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    commands = _recipe_lines(makefile, "test")
    run_idx = next(i for i, cmd in enumerate(commands) if "coverage run" in cmd)
    run_step = commands[run_idx]
    assert '--junitxml="$$data/junit.xml"' in run_step
    assert run_idx + 1 < len(commands)
    timing = commands[run_idx + 1]
    assert timing == (
        '$(BIN)/python -B tools/suite_minutes.py check "$$data/junit.xml" '
        "--budget-seconds $(TEST_BUDGET_SECONDS)"
    )
