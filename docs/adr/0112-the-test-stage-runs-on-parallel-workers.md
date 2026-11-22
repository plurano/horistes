<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0112: The test stage runs on parallel workers

- **Status**: Accepted
- **Date**: 2026-11-22
- **Authors**: Lusoris

## Context

Issue 221 asks that `make verify-all` stay under the 20-minute pre-push
deadline without dropping tests or changing coverage bounds. On this tree (2026-11-22, load 37),
the serial test stage took 17–20 minutes in pre-push gates; four of sixteen runs
were killed at the bound.

The Makefile `test` recipe still runs four coverage steps (`coverage run`,
`combine`, `report`, strict `report` total) with `fail_under` 94 in
`pyproject.toml`. Measured on the same host, `coverage run` with pytest-xdist at
eight workers finished 4077 passed and 6 skipped in 214 seconds. Combine and
report steps were unchanged because `[tool.coverage.run] patch = ["subprocess"]`
already measures xdist worker subprocesses.

Parallel runs still lost coverage total versus serial: serial reported 94.9624%
from 1043 data files; three `-n 8` runs combined 1050 files at 94.3962 / 94.6110 / 94.6110%. A
parallel run should leave 1051: the serial run's files minus its one main
process, plus the controller and eight workers. A diagnostic showed worker `gw3` left no data
file because its exit-time save never ran. Calling
`coverage.Coverage.current().save()` in each worker at `pytest_sessionfinish`
(trylast) produced 1051 files, 94.9624%, and executed lines and branches equal
to the serial run in all 26 measured files. Because the floor is 94, the loss
can fail the gate.

pytest-xdist 3.8.0 treats `--maxprocesses=0` as no cap on `-n auto`, so an
unbounded worker count is refused at the Makefile.

Issue 221 also asks for per-test timing on every test-stage run and a timing
check pinned so the budget cannot silently disappear.

## Decision

- **`TEST_WORKERS`.** The Makefile defines `TEST_WORKERS ?= 8` (scalar upper
  bound, HISS-06). The `test` target refuses values that are not whole numbers
  from 1 to 16 before any coverage step. The coverage run step invokes pytest as
  `-n auto --maxprocesses=$(TEST_WORKERS)` after `-m pytest`.

- **Pins.** `requirements/tools.in` lists `pytest-xdist`; `requirements/tools.txt`
  pins `pytest-xdist==3.8.0` with hashes. `tests/test_parallel_tests.py` and
  `tests/test_coverage_floor.py` hold the Makefile tokens and pins.

- **Worker save hook.** `tests/conftest.py` registers `pytest_sessionfinish` with
  `trylast=True`. In xdist workers only (`session.config.workerinput`), when
  `coverage` is already loaded, the hook calls `Coverage.current().save()` once
  without catching errors. pytest-xdist sends `workerfinished` after its
  `pytest_sessionfinish` hookwrapper yields (`xdist/remote.py`), so this hook
  runs before the controller tears the worker down.

- **Timing budget.** `TEST_BUDGET_SECONDS := 900` fixes the test-stage wall-time
  budget (scalar, not overridable by the environment). The coverage run step
  passes `--junitxml="$$data/junit.xml"`; the next recipe step runs
  `tools/suite_minutes.py` on that report, prints the slowest tests, and fails
  when the testsuite wall time exceeds the budget. The tool reads the report
  with defusedxml (pinned in `requirements/tools.txt`), which refuses entity
  declarations and external references. `tests/test_suite_minutes.py` pins the
  budget, the two recipe steps and the tool's exit codes.

## Consequences

- Tests must stay independent of run order and of each other; shared mutable
  state between tests can flake under xdist.

- The conftest hook must remain while the recipe runs parallel workers;
  removing it can lower the combined total below `fail_under`.

- Default eight workers targets a 32-core workstation shared with other
  builders; callers may lower `TEST_WORKERS` on smaller hosts.

- `tests/test_dev_mode.py` and `tests/test_coverage_floor.py` continue to probe
  the recipe; parallel flags are part of the pinned gate shape.

- A test stage above 900 seconds fails with the slowest tests named instead of
  relying only on the 20-minute pre-push stop.

## Alternatives considered

- **Raise the 20-minute pre-push bound.** Refused by issue 221.

- **Diff-aware test selection.** Would drop tests on some runs; refused.

- **Hand-written sharding.** More moving parts than pytest-xdist, which is the
  maintained parallel runner.

- **pytest-cov.** Would move the recipe from `coverage run` to a plugin and
  change every pinned coverage step. The one conftest hook keeps the four
  coverage steps as they are.

- **Print durations only.** Would record slow tests but enforce no budget.

- **conftest timing file.** Would hand-write what the JUnit report already
  records from pytest.

- **The standard library XML parser.** Bandit, which Codacy runs on this
  repository, flags `xml.etree.ElementTree` imports and `parse` calls (B405,
  B314). A dated `nosec` exception would need renewing; defusedxml removes the
  finding.