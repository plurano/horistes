<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0081: A person's review answers an app pull request

- **Status**: Accepted (amends [ADR-0067](0067-the-train-settles-reviews-before-landing.md))
- **Date**: 2026-11-13
- **Authors**: Lusoris

## Context

The 2026-11-13 16:13:33Z train run held lusoris/k8s pull request 7747 at CLEAN:
`review settle: review at 15:47:50Z on head, last response at 15:43:07Z`.
The app `plurano-merge-train` authored the pull request on `image-pin/kb-mcp`,
outside the dependency prefixes. Its head commit dated 15:43:07Z preceded the
only review on that head: a COMMENTED review by `lusoris` at 15:47:50Z, replying
to a Sourcery finding in a review thread. `review_settle` counted that person's
reply as an external review because its login differed from the app author's.
`test_planted_live_7747_person_reply_alone_settles` reproduces those timestamps
in [tests/test_merge_train.py](../../tests/test_merge_train.py).

[ADR-0067](0067-the-train-settles-reviews-before-landing.md) counts the author's
last push or review reply as the response. An app cannot supply the person's
reply. The GraphQL review node also lacked the author type needed to distinguish
a person from an app (`test_the_query_reads_author_and_reviews`).

## Decision

The following changes live in
[.github/workflows/merge-train.yml](../../.github/workflows/merge-train.yml):

- Fetch `author { login __typename }` on each review node, matching the pull
  request author shape (`test_the_query_reads_author_and_reviews`).
- Share bot detection through `_is_bot_author`: a GraphQL Bot type or a login
  ending in `[bot]` identifies an app; a missing or non-dictionary author does
  not. Reuse it in `is_bot_dependency`, preserving the dependency-prefix check
  (`test_is_bot_author_shapes`, `test_planted_review_settle_skips_live_bot_pr`).
- Define `_PERSON_RESPONSE` as COMMENTED and APPROVED. `_responds` counts the
  pull request author's own reviews as before. On an app-authored pull request,
  it also counts a User review in either response state. A person's request for
  changes still holds; another app's review never answers
  (`test_app_pr_person_approval_settles`, `test_app_pr_person_change_request_holds`,
  `test_app_pr_app_reply_does_not_settle`).
- In `review_settle`, use `_responds` to separate responses from external holding
  reviews on the current head. Keep the push time, default reply time and hold
  reason unchanged. A later holding review still requires another response
  (`test_planted_app_pr_person_reply_settles`,
  `test_app_pr_person_reply_before_bot_review_holds`).

## Consequences

Maintainers can release a settled app pull request by replying to a review thread
or submitting a commented or approving review on the head
(`test_planted_live_7747_person_reply_alone_settles`,
`test_app_pr_person_approval_settles`). A person's change request still holds,
and an app's review never answers another app's review
(`test_app_pr_person_change_request_holds`, `test_app_pr_app_reply_does_not_settle`).
A person-authored pull request keeps the ADR-0067 rule
(`test_person_pr_other_person_review_still_holds`).

Any person's qualifying review counts because review settle is only a timing
gate. Review-body answers still require a maintainer with write or admin rights
under [ADR-0077](0077-maintainer-answers-count-on-bot-pull-requests.md), enforced
by `review_body_hold` in [tools/merge_train.py](../../tools/merge_train.py).
Inline threads still gate landing through `mergeStateStatus`, read by
`merge_reason` in the workflow rules.

## Tests

[tests/test_merge_train.py](../../tests/test_merge_train.py) pins the reply timing
with `test_planted_app_pr_person_reply_settles`,
`test_planted_app_pr_without_reply_holds`,
`test_planted_live_7747_person_reply_alone_settles`,
`test_app_pr_person_approval_settles`, `test_app_pr_person_change_request_holds`,
`test_app_pr_app_reply_does_not_settle`,
`test_person_pr_other_person_review_still_holds` and
`test_app_pr_person_reply_before_bot_review_holds`. `test_is_bot_author_shapes`
checks missing authors, person accounts, typed app accounts and the legacy suffix;
`test_the_query_reads_author_and_reviews` pins the review-author type.
`test_planted_train_lands_app_pr_after_person_reply` and
`test_planted_train_holds_app_pr_without_reply` exercise the complete train step.
`test_planted_app_image_pin_owner_answer_lands` and
`test_app_image_pin_unrelated_owner_comment_holds` pin the app image-pin shape
for the ADR-0077 maintainer answer.
