<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0077: Maintainer answers count on bot pull requests

- **Status**: Accepted (amends [ADR-0070](0070-the-train-holds-on-unanswered-review-bodies.md))
- **Date**: 2026-11-10
- **Authors**: Lusoris

## Context

[ADR-0070](0070-the-train-holds-on-unanswered-review-bodies.md) counts review-body
answers in the pull request author's comments and reviews, or in commit messages.
An app-authored pull request can therefore remain held after a maintainer answers
its findings: the app cannot supply the author's reply. When auto-merge is armed,
the train disarms it again on each sweep.

## Decision

- Only on app-authored pull requests (`author.__typename == "Bot"`), also count
  comments and review bodies from human accounts with repository write or admin
  permission. Each answer must name the finding's review using the existing
  `pullrequestreview-<id>` or `review <id>` matching rule in
  `tools/merge_train.py`. Bot comments and reviews never count as maintainer answers.
- Keep `review_body_hold(pull, detail, right=None)` pure: the optional callback
  resolves a login's permission, and omitting it preserves the existing behavior.
  `read_review_hold` supplies `collaborator_right` for live reads.
- Check at most five distinct candidate logins per pull request in each
  `review_body_hold` call, in first-seen order. Cache each result within that
  call; candidates beyond the bound do not answer a review. The check immediately
  before landing reads permissions again.
- Read the collaborator permission endpoint with the existing installation token.
  Count only `permission` values `write` and `admin`; GitHub maps the maintain role
  to `write` and triage to `read`. A 404 means the answer does not count. Other
  refusals hold the pull request with a reason naming the login whose right could
  not be read, truncated to 200 characters. Metadata read is already granted to
  installation tokens; no app permission change is needed.
- Human-authored pull requests keep their existing answer rules and perform no
  collaborator lookup. Thread resolutions are not review-body answers: inline
  threads already gate landing through `mergeStateStatus`.

## Consequences

Maintainers can release app-authored pull requests by answering each finding with
its review identifier. Reader and non-collaborator replies leave the existing hold
reason intact. Permission failures remain visible and fail closed; an armed pull
request with a valid maintainer answer can land without being disarmed.

The bound limits additional permission reads to five per pull request per check.
Tests in `tests/test_merge_train.py` exercise write and admin answers in comments
and reviews, read and missing permissions, refused reads, the five-login bound,
bot replies, mismatched identifiers, unchanged human pull requests, and the armed
pull request deadlock.
