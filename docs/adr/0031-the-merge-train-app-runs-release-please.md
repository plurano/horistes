<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0031: The merge-train app runs release-please, and a person still merges the release

- **Status**: Accepted
- **Date**: 2026-10-14
- **Authors**: Lusoris

## Context

Release pull requests that release-please opens with GITHUB_TOKEN start no
workflow runs, so their required checks never report and the pull request
stays BLOCKED. cordanaLLM/imago#58 ("chore(main): release 0.1.0") was opened
on 2026-10-11 by `app/github-actions`. On 2026-10-14 it is BLOCKED and has
no check at all, while the rule set of that repository requires eleven
checks. A release created with GITHUB_TOKEN would not start the repository's
`release: published` workflows either (imago's `release-matrix.yml` listens
for that event).

Decision record 0029 introduced one forge app, stored once in the operations
repository, whose per-repository tokens do start workflow runs. That record
already listed release pull requests as a later use of the same app.

## Decision

- **Where.** The reusable workflow `.github/workflows/merge-train.yml` gets a
  second job, `release`, that runs after the sweep. The repositories the
  train serves keep no caller and no secret for it, and the one caller in the
  operations repository needs no change.
- **Which repositories.** The sweep already reads every repository it serves.
  Its query now also reads whether `release-please-config.json` and
  `.release-please-manifest.json` exist on the default branch. A repository
  with both files is named in the step output `releases`. A repository that
  refused in the sweep, an archived one and one not reached in time are not
  named. The list is capped at 256, the size of a job matrix, and the summary
  names any repository left over.
- **What it runs.** One matrix job per named repository, at most four at a
  time, in the environment `merge-train`:
  - `actions/create-github-app-token` mints a token of the app for that
    repository alone with `contents: write`, `pull_requests: write` and
    `workflows: write`, and revokes it when the job ends.
  - `googleapis/release-please-action` runs with that token and the
    repository's two files. It opens or updates the release pull request and,
    once a person has merged it, creates the release.
- **What it never does.** It merges nothing. The train still leaves heads that
  start with `release-please--` alone (`person_branches`), so a release stays
  a person's act in each repository.
- **Rights.** No right beyond what the app holds. The forge's permission table
  lists labels and comments on a pull request under Issues *or* Pull
  requests, and releases and references under Contents. `workflows: write` is
  needed when the release branch is rebuilt on a default branch that changed
  a workflow file.
- **Off switch.** With `switch: off` the release job does not run.
- **Proof.** `tests/test_merge_train.py` runs the sweep against the stand-in
  forge and checks the list it writes. It also checks the release job in
  the workflow file: the release-please step must take the token the app
  step minted. A copy that passes `${{ github.token }}`, or no token (which
  falls back to GITHUB_TOKEN), is found, and so are a token not limited to one
  repository, an extra right and a job that ignores the switch. imago#58 is
  the measured case of what GITHUB_TOKEN gives: no checks.

## Consequences

- **Positive**:
  - Release pull requests get their required checks and can be merged by a
    person at CLEAN.
  - A release starts the repository's `release: published` workflows.
  - A repository joins by committing the two release-please files.
- **Negative**:
  - A repository that also keeps its own release-please workflow on
    GITHUB_TOKEN has two writers on the release branch. A push from its own
    run starts no checks, so that head waits until the app pushes again. Such
    a workflow is removed once the sweep serves the repository; each
    repository's session does that.
  - Only the default file names are read. A repository with its
    configuration elsewhere is not served.
  - A release pull request is updated at the sweep's interval (every 20
    minutes), not right after each push to the default branch.
- **Neutral**: commits that release-please makes through the forge's API are
  signed by the forge. imago#58 shows this for GITHUB_TOKEN (head 42fd6413a6bf,
  verified, committer GitHub); the first run of the release job is expected
  to show the same for the app, and the report of that run says whether it does.

## References

- Fleet order g65-release-please-sweep, 2026-10-14 (paraphrased): add
  release-please to the sweep with the minted per-repository token, for both
  the release pull request and the release; the app opens and updates release
  pull requests and creates releases but never merges them; the same caller,
  no consumer secret, rights within the computed set.
- Fleet answer g65-release-please-consumer (paraphrased): release pull
  requests opened with GITHUB_TOKEN start no workflow, so required checks
  never report; the app is to be release-please's token in the reusable
  workflow.
- Decision record 0029 (`docs/adr/0029-a-forge-app-drives-the-merge-train.md`).
- Forge documentation read 2026-10-14: "Permissions required for GitHub Apps"
  (add and remove labels, create a label and comment on an issue or pull
  request: Issues or Pull requests, write; create a release and create or
  update a reference: Contents or Workflows, write); "About commit signature
  verification" (bot signatures for requests authenticated as the app).
- `googleapis/release-please-action` v5.0.0 (`action.yml` at 45996ed1f6d0:
  `token`, `repo-url`, `config-file`, `manifest-file`) and
  `actions/create-github-app-token` v3.2.0 (`action.yml` at bcd2ba492189:
  `client-id`, `private-key`, `owner`, `repositories`, `permission-*`, revoke
  in the post step). v3.3.0 of the latter was two days old on 2026-10-14,
  under the fleet's minimum age of three days.
