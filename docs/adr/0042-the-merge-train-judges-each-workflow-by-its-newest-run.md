<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0042: The merge train judges each workflow by its newest run

- **Status**: Accepted
- **Date**: 2026-10-18
- **Authors**: Lusoris

## Context

The merge train's rules module (`is_red(pull)`) returned true when any check
suite of the head commit had a conclusion in `RED` (`FAILURE`, `TIMED_OUT`,
`CANCELLED`, `ACTION_REQUIRED`, `STARTUP_FAILURE`).

Workflows that declare `concurrency: cancel-in-progress` leave cancelled check
suites next to a newer successful suite of the same workflow on the same head
commit. In cordanaLLM/nucleus pull 207 (head bfe6923), the workflow
`pr-project-gate` (workflow databaseId 355134910) left check suites
105911160001, 105911161630, and 105911346572 `CANCELLED`, while check suite
105917123100 created later finished `SUCCESS`. Because the train judged any
conclusion in `RED` across all check suites as red, the pull request was
judged red forever and never updated or landed.

Live GraphQL queries confirm that GitHub's `CheckSuite` holds `databaseId`,
`conclusion`, `createdAt`, `app { slug }`, and
`workflowRun { workflow { databaseId } }`. Suites produced by GitHub Actions
have `workflowRun` set, while suites from other apps (such as Renovate or
GitHub Advanced Security) have `workflowRun: null`. A check suite with no
completed runs has `conclusion: null`.

## Decision

- **Each workflow is judged by its newest run on the head commit.** Check
  suites with `workflowRun` set are keyed by `("workflow", workflow.databaseId)`.
- **Suites from non-Actions apps are keyed by app.** Check suites where
  `workflowRun` is null are keyed by `("app", app.slug or "")`.
- **Newest suite determined by `(createdAt, databaseId)`.** For each key, the
  suite with the greatest `(createdAt, databaseId)` tuple represents that
  workflow or app; `databaseId` breaks ties if two suites share the same
  timestamp.
- **A pull request is red only if any newest suite is red.** An older run that
  was cancelled or failed by a workflow whose newer run succeeded no longer
  holds the pull request red.
- **GraphQL checkSuites query expanded.** The train requests `databaseId`,
  `conclusion`, `createdAt`, `app { slug }`, and
  `workflowRun { workflow { databaseId } }` on the head commit's check suites.

## Consequences

- Workflows using `concurrency: cancel-in-progress` do not leave pull requests
  stuck in red when a newer run of the same workflow succeeds.
- A cancelled newest run still holds the pull request red: if the newest run
  was cancelled and no newer successful run followed, the cancellation is not
  masked.
- Check suites of other apps are keyed by app, so a newer run from the same
  app supersedes older runs of that app.
- Check suites with `conclusion: null` do not count as red.
