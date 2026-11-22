# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Suite minutes: test-stage wall time and slowest tests from a JUnit report.

    suite_minutes.py check REPORT --budget-seconds N [--slowest K]

Reads a pytest JUnit XML report (xunit2), prints the slowest test cases, and
verifies the testsuite wall time is within the declared budget. Wall time is the
testsuite ``time`` attribute (session wall clock, including under pytest-xdist),
not the sum of per-testcase times. The report is read with defusedxml, which
refuses entity declarations and external references.

Exit codes: 0 pass, 1 over budget, 2 usage error, 3 could not measure (missing,
invalid, or unreadable report).
"""

from __future__ import annotations

import argparse
import itertools
import math
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, Protocol

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as DefusedET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

MAX_TESTS = 100_000
DEFAULT_SLOWEST = 10
MAX_SLOWEST = 100


class _Node(Protocol):
    tag: str

    def get(self, key: str) -> str | None: ...

    def __iter__(self) -> Iterator[_Node]: ...


class Report(NamedTuple):
    wall_seconds: float
    tests: list[tuple[str, float]]


def _test_id(classname: str, name: str) -> str:
    if classname:
        return f"{classname}::{name}"
    return name


def _finite_time(value: str | None, label: str) -> float:
    if value is None:
        raise ValueError(f"{label} is not a finite number >= 0: {value!r}")
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError(f"{label} is not a finite number >= 0: {value!r}") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{label} is not a finite number >= 0: {value!r}")
    return seconds


def _single_testsuite(root: _Node) -> _Node:
    tag = root.tag.rsplit("}", 1)[-1]
    if tag == "testsuite":
        return root
    if tag == "testsuites":
        suites = [child for child in root if child.tag.rsplit("}", 1)[-1] == "testsuite"]
        if len(suites) != 1:
            raise ValueError(f"expected one testsuite, found {len(suites)}")
        return suites[0]
    raise ValueError("expected one testsuite, found 0")


def _read_failure(path: Path, err: OSError) -> ValueError:
    reason = err.strerror if err.strerror else str(err)
    return ValueError(f"could not read {path}: {reason}")


def read_report(path: Path, max_tests: int = MAX_TESTS) -> Report:
    """Parse a JUnit report path into wall time and per-test durations."""
    try:
        if not path.is_file():
            raise ValueError(f"no report at {path}")
    except OSError as err:
        raise _read_failure(path, err) from None
    try:
        root = DefusedET.parse(path).getroot()
    except (DefusedET.ParseError, DefusedXmlException) as err:
        raise ValueError(f"{path} is not valid XML: {err}") from None
    except OSError as err:
        raise _read_failure(path, err) from None
    suite = _single_testsuite(root)
    wall_seconds = _finite_time(suite.get("time"), "testsuite time")
    tests: list[tuple[str, float]] = []
    for case in itertools.islice(
        (c for c in suite if c.tag.rsplit("}", 1)[-1] == "testcase"),
        max_tests + 1,
    ):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        case_id = _test_id(classname, name)
        case_time = _finite_time(case.get("time"), f"test case {case_id} time")
        tests.append((case_id, case_time))
    if len(tests) > max_tests:
        raise ValueError(f"more than {max_tests} test cases")
    return Report(wall_seconds=wall_seconds, tests=tests)


def slowest(tests: list[tuple[str, float]], count: int) -> list[tuple[str, float]]:
    """Return the highest-duration tests, ties broken by id ascending."""
    ranked = sorted(tests, key=lambda item: (-item[1], item[0]))
    return ranked[:count]


def check(report: Report, budget_seconds: float) -> bool:
    """True when wall time is within the budget (equal passes)."""
    return report.wall_seconds <= budget_seconds


def _print_slowest(tests: list[tuple[str, float]], count: int) -> None:
    n = len(tests)
    k = min(count, n)
    if k == 0:
        return
    ciwf.say(f"slowest {k} of {n} tests:")
    for test_id, seconds in slowest(tests, k):
        ciwf.say(f"  {seconds:.2f}s {test_id}")


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number > 0") from err
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number > 0")
    return value


def _slowest_int(text: str) -> int:
    try:
        value = int(text, 10)
    except ValueError as err:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a whole number from 0 to {MAX_SLOWEST}"
        ) from err
    if value < 0 or value > MAX_SLOWEST:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number from 0 to {MAX_SLOWEST}")
    return value


def _run_check(args: argparse.Namespace) -> int:
    try:
        report = read_report(args.report)
    except ValueError as err:
        ciwf.complain(f"ERROR: could not measure: {err}")
        return 3
    _print_slowest(report.tests, args.slowest)
    n = len(report.tests)
    percent = 100.0 * report.wall_seconds / args.budget_seconds
    wall = report.wall_seconds
    budget = args.budget_seconds
    if check(report, budget):
        ciwf.say(
            f"PASS: test stage wall time {wall:.2f}s within the {budget:g}s budget "
            f"({percent:.0f}%), {n} tests"
        )
        return 0
    ciwf.complain(
        f"FAIL: test stage wall time {wall:.2f}s exceeds the {budget:g}s budget "
        f"({percent:.0f}%), {n} tests"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check",), help="the only command: check")
    parser.add_argument("report", type=Path, help="JUnit XML report path")
    parser.add_argument(
        "--budget-seconds",
        type=_positive_float,
        required=True,
        help="wall-time budget in seconds",
    )
    parser.add_argument(
        "--slowest",
        type=_slowest_int,
        default=DEFAULT_SLOWEST,
        help=f"number of slowest tests to print (0-{MAX_SLOWEST})",
    )
    # tests/conftest.py Tree.tool appends --root to every tool call; the report
    # path names the file, so the root is accepted and not read.
    parser.add_argument("--root", type=Path, default=Path("."), help=argparse.SUPPRESS)
    return _run_check(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
