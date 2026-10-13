<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0030: A result job reports the calling job's name

- **Status**: Accepted
- **Date**: 2026-10-14
- **Authors**: Lusoris

## Context

The forge reports the checks of a called workflow as
"<calling job> / <called job>". The licence caller's job `Licence` is reported
only as `Licence / REUSE lint`. The engine declares the required check under
the calling job's own name, `Licence`, and the rule set of `main` requires it
under that name. No run reported `Licence`, so every pull request waited.
[ADR-0025](0025-the-rule-set-goes-live-before-the-engine-fix.md) let such pull
requests merge through the maintainer's bypass, and
[ADR-0009](0009-caller-templates-in-the-engines-syntax.md) kept the calling
job out of a consumer's required checks.

Pull request #13 added a second job to this repository's own caller,
`.github/workflows/licence-check.yml`. It is named `Licence`, needs the calling
job, runs under `always()` and fails unless the call succeeded. The pinned
engine verified the rule set with it. Engine commit c444d87e6, which closes
cordanaLLM/praetor issue 1212, reads such a job as covering the calling job and
refuses it again without `always()`. Pull requests #12 and #13 then merged at
CLEAN with no bypass.

The caller template `templates/callers/licence.yml.tmpl` still rendered the
calling job alone.

## Decision

- The licence caller template holds the same job, `result`, beside the calling
  job. A consumer requires `Licence`, the name the engine declares.
- `tools/render.py check` refuses a caller that runs on pull requests, on
  `pull_request_target` or in a merge queue without that job. The job is read
  by an allow-list:
  - exactly the keys `name`, `needs`, `if`, `runs-on`, `timeout-minutes`,
    `permissions` and `steps`, so `continue-on-error` or a `uses` is refused;
  - the calling job's own name, `needs` on the calling job alone, `if: always()`
    and `permissions: {}`;
  - a timeout of 1 to 10 minutes;
  - one step with exactly `name`, `if`, `shell` and `run`, that runs in bash
    when `needs.<calling job>.result != 'success'`, prints fixed lines with
    `echo` and ends with `exit 1`.
- A caller that gates no pull request, such as the scorecard caller, needs no
  such job.
- The entry `required-check-is-reported` leaves `exceptions.yaml`: issue 1212
  is closed, and no merge uses the bypass for it any more.

This replaces the note item of the ADR-0009 decision and the bypass item of
the ADR-0025 decision. The rest of both records stands. The note rule of
`tools/render.py` stays: an entry on the required check's name in
`exceptions.yaml` again requires every template's note to name its issue.

## Consequences

- **Positive**:
  - A consumer's licence check blocks a pull request through the rule set the
    engine renders, with no bypass.
  - A cancelled or skipped licence call fails `Licence`, because the step runs
    on every result other than success.
- **Negative**:
  - Each pull request starts one more job of a few seconds.
  - The job is a fixed shape; a consumer that wants another one changes this
    template and its check.
- **Neutral**: no consumer calls the licence caller yet (measured 2026-10-14
  over the 72 repositories of the fleet's nine accounts), so no consumer pull
  request waits on this change.

## References

- Fleet answer ledger, id g65-create-app-report-judged, 2026-10-14
  (paraphrased): after the merge train lands, apply the result job of pull
  request #13 to the licence caller template with a planted case, and list the
  consumers of the caller and their pull requests that wait on `Licence`.
- Pull request #13 (commit 649ab51): the result job in this repository's own
  caller, checked with the pinned engine and with engine commit c444d87e6.
- `tests/test_render.py`: `PLANTED_RESULT` holds sixteen planted result jobs,
  each refused with its own message; a caller without one is refused on each
  gating trigger and accepted on push alone. Twelve mutants of
  `tools/render.py`, each taking one rule out, are caught, and the cases fail
  against `tools/render.py` of commit 8748926.
