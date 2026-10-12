<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0027: A licence run narrowed to a folder is named for that folder

- **Status**: Accepted
- **Date**: 2026-10-12
- **Authors**: Lusoris

## Context

The input `root` of `.github/workflows/licence.yml` names the folder the
licence tool checks as the project root. The workflow refuses a root that is
no plain relative path, but any folder of the repository passes. A run with
`root: docs` checks `docs` only, and its check was still reported as
`<calling job> / REUSE lint`, the name a consumer requires for the whole
repository. A caller could so turn the required check green while most files
went unchecked. The gap audit of 2026-10-12 listed this as a gate a caller can
skip (row A2-03).

The input stays: the planted fixtures of
[ADR-0006](0006-planted-case-as-a-fixture.md) are run through it.

## Decision

- The licence job is named `REUSE lint` when `root` is `.`, and
  `REUSE lint (<root>)` otherwise. A narrowed run therefore never reports the
  check a consumer requires.
- A step after the root check writes the scope to the run's summary: the whole
  repository, or the folder with the note that files outside it were not
  checked.
- The changed check name is a break under
  [ADR-0026](0026-a-call-that-analyses-nothing-fails.md);
  `interface.lock.json` moves to 0.2. A caller that passes no `root` sees no
  change.

## Consequences

- **Positive**: a required `REUSE lint` is green only for a run of the whole
  repository; a narrowed run states its scope in its name and its summary.
- **Negative**:
  - A caller that narrows the root on purpose reports a new check name and has
    to name it so in its required checks, if it requires it at all.
  - Another spelling of the whole repository, such as `./`, is reported as a
    narrowed run. That fails towards red.
  - The job starts one more step.
- **Neutral**: the trial map of the same-verdict check names a fixture pair by
  the reported name, which now carries the fixture folder; `tools/verdict.py`
  reads the called job's name as an expression and still finds the job.

## References

- Fleet answer ledger, id horistes-licence-root-scope, 2026-10-12
  (paraphrased): a narrowed run is named with its folder and its summary names
  the scope; the planted case is a narrowed run, which must not satisfy the
  required check.
- Fleet order fleet-green-while-broken-20261012 (paraphrased): fix the gates a
  caller can skip, each shown failing on a planted case first.
- `tests/test_licence_scope.py`: four narrowed roots must not report
  `REUSE lint`; the whole repository must; with a fixed name, the narrowed run
  reports the required check again. The scope step is run for each root.
  Against the workflow before this record, the four planted cases failed.
- `tests/expressions.py`: the reader of workflow expressions those tests play
  the name with, shared with `tests/test_scorecard_calls.py` and tested in
  `tests/test_expressions.py`.
