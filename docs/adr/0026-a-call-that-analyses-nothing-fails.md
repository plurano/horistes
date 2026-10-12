<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0026: A scorecard call that analyses nothing fails

- **Status**: Accepted
- **Date**: 2026-10-12
- **Authors**: Lusoris

## Context

Both inputs of `.github/workflows/scorecard.yml` default to false. A caller
that keeps the defaults, or that passes `outside_hosts` by visibility in a
repository that is not public, ran no job at all: the guard of
[ADR-0015](0015-refusal-outside-a-public-repository-is-a-failed-job.md) and the
analysis were both skipped. The forge reports a skipped job as a success, so
that run looked green and analysed nothing. The template of
[ADR-0013](0013-scorecard-behind-a-switch.md) made this the normal case for
every private caller, and ADR-0015 kept it on purpose ("both jobs are skipped
and nothing fails"). The gap audit of 2026-10-12 listed it as a gate a caller
can skip (row A2-05).

Two facts of the forge shape the fix:

- A check is named by the job's `name`, and that name may be an expression
  of the inputs. Two jobs of one run must not report the same name.
- A job whose needed job failed or was skipped is skipped too, unless its
  condition has a status function, and the hosts rule of ADR-0015 forbids
  status functions.

## Decision

- With `outside_hosts` false, a job `switched_off` runs, grants no scope, and
  fails with one error line and the same text in the run's summary. It is
  named `Analysis` when it runs. The analysis job is named `Analysis` when
  `outside_hosts` is true and `Analysis (not started)` when it is false. So the
  check `<calling job> / Analysis` is the analysis when it runs and a failure
  with the reason when it does not.
- The guard `visibility` loses its condition and runs on every call. It is
  given the visibility and both inputs. With both inputs false it exits 0;
  otherwise it refuses a repository that is not public as before. Because it
  never skips, `switched_off` can need it without a status function.
- `tools/hosts.py check` holds every workflow with a host marked `public_only`
  to this: the guard has no condition, and one of its steps that runs a
  command is given the visibility expression and every input that switches on
  a marked host, as `${{ inputs.<name> }}`. This replaces the clause of
  ADR-0015 under which the guard's condition is exactly those inputs joined by
  `||`. The rest of ADR-0015 stands.
- The template is rendered in public repositories only. This replaces the
  clause of ADR-0013 under which a private repository keeps the caller and
  its job is skipped until the repository turns public. The rest of ADR-0013
  stands, the expression the template passes included.
- While `interface_major` is 0, a break raises `interface_minor` instead.
  `tools/interface.py check` counts a break as declared when the major version
  rose, or when both versions are at major 0 and the minor version rose. From
  major 1 on, only a higher major version declares a break. This amends the
  clause of [ADR-0002](0002-versioning-and-pinning.md) on `interface_major`.
  The renamed check is such a break; `interface.lock.json` moves to 0.1.

## Consequences

- **Positive**:
  - A call that analyses nothing is a failed check with its reason, never a
    green run.
  - A run is green only when the analysis ran in a public repository
    (`tests/test_scorecard_calls.py`, every combination of the two inputs and
    three visibilities).
  - A break before the first release must still be declared; the interface
    check does not pass while broken.
- **Negative**:
  - A caller in a repository that is not public is always red. A caller that
    passes a literal `false` is always red.
  - The check name `Analysis` now belongs to the refusal when the switch is
    off. A consumer that reads the check names sees `Analysis` and
    `Analysis (not started)`.
  - Whether the event of a scheduled run carries the repository's visibility
    is still not measured (ADR-0013). If it does not, a rendered caller in a
    public repository turns red on its schedule instead of skipping.
  - When the guard fails, the skipped analysis job still reports as
    `Analysis`, so `<calling job> / Analysis` reads as skipped beside the
    failed `Public repository`. The run is red through the guard.
  - The guard starts one runner on every call.
- **Neutral**: the texts of ADR-0002, ADR-0013 and ADR-0015 stay as written;
  this record is where their changed clauses stand now.

## References

- Fleet answer ledger, id horistes-scorecard-skip, 2026-10-12 (paraphrased):
  with `outside_hosts` false the no-analysis path fails an `Analysis` job with
  the reason; the template renders the caller for public repositories only; a
  superseding record replaces the caller part of ADR-0013; the planted case is
  a caller with the default input, which must turn red.
- Fleet answer ledger, id horistes-scorecard-check-name, 2026-10-12
  (paraphrased): the analysis job is named by input; `interface_major` stays
  0; the change is marked as breaking with a migration note; ADR-0002 is
  amended so that a break while the major version is 0 raises the minor
  version.
- Fleet order fleet-green-while-broken-20261012 (paraphrased): fix the gates a
  caller can skip, each shown failing on a planted case first.
- `tests/test_scorecard_calls.py`: plays the jobs of the real workflow the way
  the forge plays a call. The planted caller with the default inputs turns
  red; with the job `switched_off` removed, the same call reads green.
- `tests/test_guard.py`: the changed hosts rule on fixtures, one planted
  defect per clause, and the guard's command run for every input it refuses.
- `tests/test_interface.py`: a break at 0.0 fails, and raising either version
  declares it; from 1.0 on, only a higher major version does.
- actionlint 1.7.12, the version the gate installs: its table of contexts
  allows `github`, `inputs`, `matrix`, `needs`, `strategy` and `vars` in
  `jobs.<job_id>.name`. The forge's documentation on `needs` and on skipped
  jobs is quoted in ADR-0015. Nothing here ran on the forge yet; no consumer
  calls the workflow.
