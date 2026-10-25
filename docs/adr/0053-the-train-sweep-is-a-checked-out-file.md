<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0053: The train sweep is a checked-out file

- **Status**: Accepted
- **Date**: 2026-10-25
- **Authors**: Lusoris

## Context

The engine audit caps a run script at 4096 fields. The sweep step of
`.github/workflows/merge-train.yml` ("Land or update one pull request in every
repository of the app") ran an inline Python script that stood close to that
cap ([ADR-0034](0034-the-merge-train-queues-where-the-branch-has-a-merge-queue.md)).
Twenty more words took the audit from pass to
`run script exceeds 4096 fields`, so every further fix of the train had to be
paid for by compacting code. The cap itself stays as it is.

## Decision

- The sweep lives in `tools/merge_train.py`; the step runs
  `python3 "$GITHUB_WORKSPACE/merge-train-source/tools/merge_train.py"` with
  the same environment and outputs as before.
- A step before it checks out that one file from the workflow's own commit,
  `job.workflow_repository` at `job.workflow_sha`, so a caller pinned to a
  commit runs that commit's script and never the caller's tree. The checkout
  keeps no credentials.
- The job's token holds no permission, because callers grant none. The
  repository is public, so the checkout needs none: a public repository's job
  with `permissions: {}` checks out its own tree, and the gate here checks out
  the public Praetor source with a token of another repository. A failed
  checkout stops the train run before the sweep step.
- actionlint 1.7.12, the newest release, does not know `job.workflow_*`. The
  finding is accepted for `.github/workflows/merge-train.yml` alone in
  `.github/actionlint.yaml`, with a day it ends. `tools/exceptions.py` now
  reads that file as well and refuses an entry without a reason and a day, an
  expired one, one more than 90 days ahead and a key that is a pattern rather
  than one file ([ADR-0007](0007-one-own-exception-list.md)).

## Consequences

- The sweep script no longer counts against the cap of a run script; the
  rules module written by the step before it still does.
- `tests/test_merge_train.py` loads the script from `tools/merge_train.py` and
  refuses a checkout of another repository or commit, one that keeps
  credentials, one that fetches more than the script and one placed after the
  sweep step.
- When actionlint learns `job.workflow_*`, the ignore entry goes; its day
  forces that question by 2027-01-20 at the latest.
