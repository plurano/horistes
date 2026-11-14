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

- Fetch `author { login __typename }`, `commit { oid }`, `body` and
  `comments(first: 10) { totalCount nodes { replyTo { id } } }` on each review
  node in `QUERY` (`test_the_query_reads_author_and_reviews`).
- Share bot detection through `_is_bot_author`: a GraphQL Bot type or a login
  ending in `[bot]` identifies an app; a missing or non-dictionary author does
  not. Reuse it in `is_bot_dependency`, preserving the dependency-prefix check
  (`test_is_bot_author_shapes`, `test_planted_review_settle_skips_live_bot_pr`).
- Define `_person_reply` for User reviews: APPROVED answers; COMMENTED answers
  only with an empty or whitespace-only body and a nonempty, complete comments
  page containing thread replies only. `totalCount` must equal the fetched node
  count, and every comment must have a truthy `replyTo`. Missing comments,
  truncated pages, new inline comments and new review bodies do not answer
  (`test_person_reply_shapes`, `test_app_pr_truncated_comments_hold`).
- On app-authored pull requests, `_responds` delegates only to `_person_reply`.
  The authoring app's own review never answers. On person-authored pull requests,
  the author's login still identifies a response
  (`test_planted_app_pr_own_app_review_does_not_settle`).
- Define `_standing_change_request` to return the newest User change request on
  the head without a strictly later APPROVED review by the same login, or None.
  A different person's reply or approval cannot clear it
  (`test_planted_app_pr_change_request_survives_other_reply`,
  `test_planted_app_pr_change_request_survives_other_approval`).
- Read the latest 100 approvals and change requests in a separate list, so thread
  replies cannot push a change request out of the read. Hold an app pull request
  when that list has more entries than were read.
- In `review_settle`, use `_on_head_reviews` and `_review_timing_hold` to compare
  holding reviews with responses classified by `_responds` on the current head.
  A holding review at or after the last push or qualifying
  reply holds every pull request. This amends ADR-0067's strict-later rule: a tie
  now holds. Keep the timing reason unchanged; only after that check passes on
  an app pull request, report any `_standing_change_request`
  (`test_planted_app_pr_tie_holds`, `test_planted_person_pr_tie_holds`).

## Consequences

Maintainers can release a settled app pull request by submitting a review on the
head containing only thread replies, or by approving it
(`test_planted_live_7747_person_reply_alone_settles`,
`test_app_pr_person_approval_settles`). A new person review holds like any other
holding review, until answered by a later qualifying response
(`test_planted_app_pr_person_standalone_review_holds`,
`test_app_pr_standalone_review_answered_by_reply_settles`).

A person's change request stands until that person approves later or the review
is dismissed: a DISMISSED review does not hold. An approval before the change
request does not clear it (`test_app_pr_change_request_cleared_by_same_person_approval`,
`test_app_pr_approval_before_change_request_holds`). An app's review, including
the authoring app's own review, never answers on an app pull request
(`test_app_pr_app_reply_does_not_settle`,
`test_planted_app_pr_own_app_review_does_not_settle`). A timestamp tie holds on
every pull request (`test_planted_app_pr_tie_holds`,
`test_planted_person_pr_tie_holds`). Other person-authored pull request behavior
keeps the ADR-0067 rule (`test_person_pr_other_person_review_still_holds`).

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

The stricter reply classification and standing change requests are pinned by
`test_planted_app_pr_own_app_review_does_not_settle`,
`test_planted_app_pr_tie_holds`, `test_planted_person_pr_tie_holds`,
`test_planted_app_pr_change_request_survives_other_reply`,
`test_planted_app_pr_change_request_survives_other_approval`,
`test_app_pr_change_request_cleared_by_same_person_approval`,
`test_app_pr_approval_before_change_request_holds`,
`test_planted_app_pr_person_standalone_review_holds`,
`test_planted_app_pr_person_new_inline_comment_holds`,
`test_app_pr_reply_mixed_with_new_comment_holds`,
`test_app_pr_truncated_comments_hold`,
`test_app_pr_standalone_review_answered_by_reply_settles` and
`test_person_reply_shapes`. The query test also pins the body and thread-reply
fields, including the comment count needed to reject truncated pages.

The separate opinions list and its completeness hold are pinned by
`test_planted_app_pr_change_request_survives_review_window`,
`test_planted_app_pr_truncated_opinions_hold`,
`test_app_pr_complete_opinions_boundary_settles`,
`test_app_pr_dismissed_change_request_settles`,
`test_person_pr_truncated_opinions_do_not_hold` and
`test_opinions_query_reads_bounded_review_states`.
