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
  the pull request node:
  ```graphql
  reviews(last: 20) {
    nodes {
      submittedAt
      state
      author { login }
      commit { oid }
    }
  }
  ```
- `review_settle()` inspects submitted reviews on the pull request's current head commit
  (`commit.oid == headRefOid`):
  1. Bot pull requests whose branch matches `conf["dependency"]` are exempt and continue
     using their existing minimum-age check ([ADR-0046](0046-the-merge-train-counts-the-minimum-age-from-the-pull-request.md)).
  2. Reviews authored by the pull request author (`author.login == pr_author`) count as
     author replies.
  3. Reviews authored by external accounts (`author.login != pr_author`) are considered.
     If no external reviews exist on the current head, the pull request does not wait.
  4. When an external review on the head is newer than the author's last response
     (`settle_at = max(push_at, reply_at)` where `push_at` is the head commit's
     `committedDate` and `reply_at` is the latest review by the PR author on the same head),
     `review_settle()` returns a hold reason:
     `f"review settle: review at {newest_ext:%H:%M:%SZ} on head, last response at {settle_at:%H:%M:%SZ}"`.
- `verdict()` integrates `review_settle()` immediately after `too_young()` and before
  the accept/behind state checks.
- The mechanism is self-contained within the verdict calculation rather than implemented as
  a required status check, because it uses the forge's existing review and commit data and
  does not rely on external sessions or runners posting statuses.

## Consequences

Person-authored pull requests at `CLEAN` or `UNSTABLE` wait for the author to respond to
every review event on the current head (by pushing a new commit or posting a review comment)
before landing. Once the author replies or pushes a fix, the train resumes landing them normally.
Bot-authored dependency updates remain unaffected.

Tests in `tests/test_merge_train.py` verify that an unaddressed review holds a person-authored
pull request, an author reply on the same head unblocks landing, bot pull requests bypass review
settle, and the GraphQL query fetches the required author and review fields.
