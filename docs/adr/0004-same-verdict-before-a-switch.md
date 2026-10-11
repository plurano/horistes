<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0004: A consumer switches only after the same verdict is shown

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

Moving a repository from its own job to a shared workflow must not change what
the job decides. A new job that is green proves little: it may skip, run on
other events, or fail for a reason unrelated to the defect it guards against.
The old job is the only reference, and it disappears with the switch.

## Decision

- Before a repository's own job is removed, old and new job run side by side
  on the same commits and events. `tools/verdict.py` compares them and the
  switch needs its exit code 0.
- A row is one pair of jobs on one triggering event of one commit. It counts
  as equal only when both jobs decided (pass or fail) and decided the same.
  Skipped, cancelled, running, absent, flaky and draft-stopped jobs count for
  nothing; an absent job, an unknown conclusion and a failed request to the
  forge are errors, never a pass.
- A pair needs at least ten equal rows on distinct subjects, one of them an
  ordinary commit both jobs pass, and the planted cases the trial's map names:
  at least one commit both must fail, with a reason marker found in both
  failures, and at least one both must pass.
- The new job may not ask for more than the old one: no further write scope,
  secret, host or outside action, the same events and the same kind of runner.
- The check reads the forge with GET requests only, or a recorded file of the
  same shape. The table it writes is the evidence kept with the switch.
- Only jobs whose result is a read-only verdict are compared this way. Jobs
  that publish, release or write to the repository need their own proof.

Exit codes: 0 pass, 1 a verdict, a reason or a side condition differs,
2 the evidence does not carry the switch, 3 input or data error.

## Consequences

- **Positive**: a switch is a measured statement about the old and the new
  job on named commits; a silent loss of a check shows as a missing or
  unmapped row.
- **Negative**: every switch costs a trial branch with planted commits and
  both jobs running twice for its duration; a failing step leaves little text
  on the forge, so the reason marker often can only name the step that failed.

## References

- Fleet answer ledger, id shared-ci-start (paraphrased): each switch of a
  consumer ships the same-verdict check with its planted case, and that case
  is seen failing once.
