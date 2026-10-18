<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0041: The merge train refreshes a draft-stopped pull request

- **Status**: Accepted
- **Date**: 2026-10-18
- **Authors**: Lusoris

## Context

On cordanaLLM/nucleus pull 205 a push landed seconds before the pull request
was marked ready for review. The workflow's concurrency group cancels the
older run per head ref, and the `synchronize` run, whose payload still said
`draft: true`, was delivered after the `ready_for_review` run and cancelled
it. The surviving run failed at the step `Stop on a draft pull request`
(run 39082288216).

A re-run replays the event payload of the run, so attempt 2 failed at the
same step. Only a fresh event fixed it: the label was removed and added
again, and run 39082676948 passed. Until then the pull request sat red, and
the train left it alone in every sweep.

## Decision

- **A fresh event, never a re-run.** The train adds the label
  `merge-train: refresh` to such a pull request and removes it at once. The
  `labeled` and `unlabeled` events carry the pull request as it is now. The
  app's installation token sends them, so they start workflow runs, which
  events of GITHUB_TOKEN would not.
- **Only where nothing else is red.** Candidates are pull requests whose
  verdict is red BEHIND, BLOCKED or UNSTABLE, oldest first, at most three per
  repository and sweep. Each workflow is judged by its newest run on the head
  commit, as in decision record 0042. The train reads the jobs of every red
  run and refreshes only when the failed steps of each of them are the draft
  step and nothing else. A red suite of an app without a workflow run, or any
  other failed step, leaves the pull request alone: a person or a push fixes
  it.
- **Once per failed run.** The query reads the last five removals of the
  label. When one is as new as the newest red run or newer, the refresh
  started no run (the workflow has no label trigger), and the train names
  that in its summary instead of toggling again.
- **Same place in the order.** The refresh is the run's one write: after a
  landing and the in-flight hold, before arming and updating a BEHIND pull
  request.
- **A label already gone counts as removed** (404 on the removal).

## Consequences

- **Positive:** a pull request stopped by the draft race lands on a later
  sweep without a person.
- **Negative:** a workflow without `labeled` or `unlabeled` among its
  `pull_request` types is not restarted; the summary says a person pushes.
  The label shows briefly in the pull request's timeline.
- **Neutral:** the token rights stay as they are: `pull_requests: write`
  covers label writes on a pull request, and `actions: read` covers the jobs
  read.

## References

- Decision record 0042
  (`docs/adr/0042-the-merge-train-judges-each-workflow-by-its-newest-run.md`).
- Decision record 0029 (`docs/adr/0029-a-forge-app-drives-the-merge-train.md`).
