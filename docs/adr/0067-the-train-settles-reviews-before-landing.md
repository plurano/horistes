<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0067: The train settles reviews before landing

- **Status**: Accepted
- **Date**: 2026-11-08
- **Authors**: Lusoris

## Context

The merge train periodically sweeps every repository the app is installed on, landing
the oldest clean pull request ([ADR-0029](0029-a-forge-app-drives-the-merge-train.md)).
The train's existing `verdict()` checks merge state, labels, draft status, and age, but
not review activity.

In two observed cases, the train landed a person-authored pull request while review
comments were being addressed:
- horistes #120 merged 2026-11-07 16:45 CET while a CodeRabbit fix was in progress
  (follow-up #121).
- saeptum #37 merged on CLEAN 3 minutes after a Codacy review body (follow-up #38).

Coordination must remain self-contained, stateless, and forge-driven without introducing
external session dependencies or requiring extra status checks.

## Decision

- Add a settle rule in `train_rules.py`: `review_settle(pull, conf, now)`.
- The GraphQL pull request query fetches `author { login }` and `reviews(last: 20)` on
  the pull request node.
- `review_settle()` inspects submitted reviews on the pull request's current head commit.
  Bot pull requests (dependency prefix branch AND `[bot]` author suffix) are exempt and
  continue using their existing minimum-age check
  ([ADR-0046](0046-the-merge-train-counts-the-minimum-age-from-the-pull-request.md)).
  Only `CHANGES_REQUESTED` and `COMMENTED` review states hold; approved, dismissed, and
  pending reviews are ignored.  When a blocking external review on the head is newer than
  the author's last push or review reply, `review_settle()` returns a hold reason.
- `verdict()` integrates `review_settle()` immediately after `too_young()` and before
  the accept/behind state checks.
- The mechanism is self-contained within the verdict calculation rather than implemented as
  a required status check, because it uses the forge's existing review and commit data and
  does not rely on external sessions or runners posting statuses.

## Consequences

Person-authored pull requests at `CLEAN` or `UNSTABLE` wait for the author to respond to
every blocking review on the current head (by pushing a new commit or posting a review)
before landing. Approved and dismissed reviews do not hold. A person-authored pull request
on a dependency-prefix branch is not exempt. Once the author responds, the train resumes
landing normally. Bot-authored dependency updates remain unaffected.

Tests in `tests/test_merge_train.py` verify that an unaddressed review holds a person-authored
pull request, an author reply on the same head unblocks landing, bot pull requests bypass review
settle, approved reviews do not hold, pending reviews do not crash the sweep, a person on a
dependency-prefix branch is held, and the GraphQL query fetches the required fields.
