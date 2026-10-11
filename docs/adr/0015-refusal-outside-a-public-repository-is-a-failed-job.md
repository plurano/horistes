<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0015: A workflow with an input for public repositories only refuses another repository with a failed job

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

[ADR-0013](0013-scorecard-behind-a-switch.md) put the scorecard run behind an
input that a rendered caller sets by the repository's visibility, and left one
case open: a caller written by hand in a private repository can pass a plain
`true`, and the called workflow then runs and asks two outside hosts with the
repository's name. The called workflow has to refuse that case itself. There
were three ways to do it.

- **Skip the analysis.** A condition on the job is the smallest change. The
  forge reports a skipped job as a success, to a required check as well, so
  the person who wrote `true` sees a green run that analysed nothing. That is
  a fallback nobody is told about.
- **A step in the analysis job that stops.** The Scorecard service reads the
  called file before it takes a published result and refuses a job with a
  step that uses no action (`verify_workflow.go`, see References). A `run`
  step in that job stops every caller's publication.
- **A job of its own that fails.** The service's rules on steps, `env` and
  `defaults` bind the job that runs the action. On any other job it checks one
  thing, that the job does not write `id-token`.

## Decision

- `.github/workflows/scorecard.yml` has a job `visibility`, reported as
  `<calling job> / Public repository`. It runs when `outside_hosts` or
  `publish` is true, grants no scope, and has one step. The step is given
  `${{ github.event.repository.visibility }}` as an environment variable,
  exits 0 when the value is `public`, and otherwise writes one error line and
  the same text to the run's summary and exits 1. The value names the calling
  repository: in a called workflow the `github` context is the caller's.
- The refusal fails closed. Private, internal, an empty value and any other
  spelling fail; only `public` passes.
- The analysis job needs `visibility` and keeps its own condition. A job whose
  needed job failed is skipped unless its condition holds a status function,
  so the action does not start and no outside host is asked.
- This is a rule for every reusable workflow, held by `tools/hosts.py check`
  (`make check-hosts`): a workflow with a host marked `public_only` in
  `hosts.toml` has a job `visibility` whose condition is exactly the inputs
  that switch on a marked host, joined by `||`. That job has a step that runs
  a command and is given the visibility expression, sets no
  `continue-on-error` and needs no other job. Every other job needs it and
  has no `always()`, `failure()` or `cancelled()` in its condition.
- This replaces the sentence of ADR-0013 under which the called workflow does
  not check the visibility itself. The rest of that record stands: a rendered
  caller still passes the input by visibility, so in a repository that is not
  public both jobs are skipped and nothing fails.

## Consequences

- **Positive**:
  - A plain `true` in a repository that is not public is a failed check with
    its reason in the annotation and the summary, and the repository's name
    reaches no outside host.
  - A skipped scorecard run now has one meaning: the caller's switch is off.
  - A second workflow with a marked host cannot be added without the job; the
    gate refuses the file.
- **Negative**:
  - A run that is switched on starts one more runner for one step.
  - A caller's job reports two checks. `<calling job> / Public repository` is
    new; `interface.lock.json` records it and `make check-interface` counts an
    added job as no break.
  - `publish: true` with `outside_hosts` off fails in a repository that is not
    public although no host would be asked, because the analysis does not run
    without `outside_hosts`. The call is wrong either way and is told so.
  - Whether the event of a scheduled run carries the repository's visibility
    is still not measured (ADR-0013). If it does not, a caller written by hand
    with a plain `true` in a public repository fails on its schedule. A
    rendered caller is not touched: its expression is then false and both
    jobs are skipped.
  - The rule reads the shape of the job from the text. That the command
    refuses is tested for the workflow of this repository, whose step the
    tests run; a second workflow needs the same test of its own.
  - Nothing here ran on the forge. This repository is private, so no consumer
    can call the workflow; that a failed `visibility` skips the analysis rests
    on the forge's documentation and shows with the first public consumer.
- **Neutral**: the text of ADR-0013 stays as written; this record is where
  its open case closes.

## References

- Fleet answer ledger, id shared-ci-scorecard-private-guard, 2026-10-11
  (paraphrased): the called workflow gets a guard job that fails with a stated
  reason when an input for public repositories is true in a private one; under
  the rule against silent fallback a skipped job reads as a pass; one job and
  one step are the accepted price.
- GitHub Docs, read 2026-10-11. Reusing workflows: "the `github` context is
  always associated with the caller workflow." Workflow syntax,
  `jobs.<job_id>.needs`: "If a job fails or is skipped, all jobs that need it
  are skipped unless the jobs use a conditional expression that causes the
  job to continue." Evaluate expressions, status check functions: "A default
  status check of `success()` is applied unless you include one of these
  functions." Control jobs with conditions: "A job that is skipped will
  report its status as "Success". It will not prevent a pull request from
  merging, even if it is a required check."
- ossf/scorecard-infra, `api/app/server/verify_workflow.go` at commit
  `ffe0a11789f8` (2026-09-28), read 2026-10-11: the job that runs the action
  may hold only steps with `uses`; another job is refused only for
  `id-token: write`.
- `tests/test_guard.py`: the rule on fixtures, with one planted defect per
  clause; the workflow of this repository with a weakened guard; the command
  of the real step, run for `public` and for six other values.
- Each check was seen to refuse on 2026-10-11: nine changes planted in a copy
  of the tree, among them the workflow as it was before this record and the
  rule as it was before it, each failed the check that covers it. The log is
  not part of the tree.
