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
the train disarms it again on each sweep (`review_body_hold` and
`disarm_red_scanning` in [tools/merge_train.py](../../tools/merge_train.py)).

## Decision

- Only on app-authored pull requests (`author.__typename == "Bot"`), also count
  comments and review bodies from human accounts with repository write or admin
  permission. Each answer must name the finding's review using the existing
  `pullrequestreview-<id>` or `review <id>` matching rule in
  `_is_review_answered` in [tools/merge_train.py](../../tools/merge_train.py).
  Bot comments and reviews never count as maintainer answers (`_answer_candidates`).
- Keep `review_body_hold(pull, detail, right=None)` pure: the optional callback
  resolves a login's permission, and omitting it preserves the existing behavior.
  `read_review_hold` supplies `collaborator_right` for live reads.
- Check at most five distinct candidate logins across all unanswered reviews per
  `review_body_hold` call (`ANSWER_RIGHTS` and `_maintainer_unanswered`). Cache each
  result within that call; candidates beyond the bound do not answer a review.
  `late_review_hold` reads permissions again immediately before landing.
- Order candidate logins by their best `authorAssociation` across all answers:
  OWNER, MEMBER and COLLABORATOR first, CONTRIBUTOR next, everything else or a
  missing association last. Preserve first-seen order within each rank
  (`ANSWER_ASSOCIATION_RANK`, `ANSWER_UNRANKED` and `_ranked_logins`). Association
  only orders reads; the permission read decides whether an answer counts.
  Association can understate
  a maintainer: an organisation member whose membership is private shows as
  CONTRIBUTOR to a reader outside the organisation. Measured on 2026-11-10 on
  comment 6767657578 of this repository, whose author is a member: the author's own
  token reads `MEMBER`, and an anonymous
  `curl -sS https://api.github.com/repos/plurano/horistes/issues/comments/6767657578`
  reads `"author_association": "CONTRIBUTOR"`. The
  [GraphQL enum](https://docs.github.com/en/graphql/reference/enums#commentauthorassociation)
  describes association, not repository permission.
- Read the collaborator permission endpoint with the existing installation token.
  Count only `permission` values `write` and `admin`; GitHub maps the maintain role
  to `write` and triage to `read`. A 404 means the answer does not count. Other
  refusals hold the pull request with a reason naming the login whose right could
  not be read, truncated to 200 characters (`collaborator_right` and
  `review_body_hold`; REST references below). The endpoint needs repository
  Metadata read. A token minted with a `permissions` body holds the permissions it
  names, so `WRITE_RIGHTS` and `READ_RIGHTS` name `metadata: read` rather than rely
  on GitHub adding it to a narrowed token. The app already holds metadata read in
  [apps/merge-train.json](../../apps/merge-train.json), so no installation has to
  accept a new permission.
- Human-authored pull requests keep their existing answer rules and perform no
  collaborator lookup. Thread resolutions are not review-body answers: inline
  threads already gate landing through `mergeStateStatus`. `run` in
  [tools/merge_train.py](../../tools/merge_train.py) calls the imported `verdict`,
  whose `merge_reason` reads that field in the rules module embedded in
  [.github/workflows/merge-train.yml](../../.github/workflows/merge-train.yml).

## Consequences

Maintainers can release app-authored pull requests by answering each finding with
its review identifier. Reader and non-collaborator replies leave the existing hold
reason intact. Permission failures remain visible and fail closed; an armed pull
request with a confirmed maintainer answer can land without being disarmed
(`test_maintainer_answer_keeps_armed_bot_pull_request_armed`). A transient refusal
holds the pull request and disarms it if armed: the forge's auto-merge would
otherwise land it once checks pass while the answer remains unconfirmed
([ADR-0070](0070-the-train-holds-on-unanswered-review-bodies.md),
`disarm_red_scanning` and `late_review_hold`). The next run with a working permission
read lands it through `land_clean`, costing one run and requiring no manual
re-arm (`test_maintainer_right_refusal_disarms_then_next_run_lands`).

The bound limits additional permission reads to five per pull request per check.
Five accounts with equal or better association than the maintainer that name the
review first can still exhaust the budget. The pull request stays held with its
visible unanswered-review reason (`test_maintainer_right_lookups_stop_at_five` and
`test_maintainer_none_writer_after_five_contributors_stays_held`).

## References

- [Get repository permissions for a user](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user):
  permission values, maintain mapped to write, triage mapped to read, installation
  token support and required repository Metadata read permission.
- [Create an installation access token for an app](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app):
  the `permissions` request body selects the token's permissions.
- [CommentAuthorAssociation](https://docs.github.com/en/graphql/reference/enums#commentauthorassociation):
  the association values used to order candidates.
- Tests below live in [tests/test_merge_train.py](../../tests/test_merge_train.py)
  and map the Decision claims to executable checks:
  - App authors, permission values and answer locations:
    `test_maintainer_write_answer_lands_bot_pull_request`,
    `test_maintainer_admin_answer_lands_bot_pull_request`,
    `test_maintainer_review_answer_lands_bot_pull_request`,
    `test_maintainer_read_right_cannot_answer_bot_review`,
    `test_maintainer_missing_right_cannot_answer_bot_review`,
    `test_bot_maintainer_answer_does_not_count`,
    `test_bot_maintainer_review_answer_does_not_count`,
    `test_maintainer_answer_for_different_review_is_not_a_candidate`.
  - Pure callback and unchanged human rules:
    `test_bot_review_body_without_right_callback_keeps_old_behaviour`,
    `test_maintainer_answer_does_not_release_human_pull_request`,
    `test_author_comment_or_review_naming_id_allows_landing`,
    `test_commit_message_naming_review_id_allows_landing`.
  - Bound, cache, global ordering and query fields:
    `test_maintainer_right_lookups_stop_at_five`,
    `test_maintainer_right_cache_spans_reviews_and_reuses_fifth_login`,
    `test_maintainer_false_right_is_cached_and_remaining_count_recomputed`,
    `test_maintainer_collaborator_answer_precedes_five_none_commenters`,
    `test_maintainer_best_association_spans_answers`,
    `test_maintainer_private_member_contributor_answer_precedes_none`,
    `test_maintainer_none_writer_after_five_contributors_stays_held`,
    `test_maintainer_member_on_later_review_gets_global_priority`,
    `test_review_bodies_query_fields`.
  - Explicit repository token metadata and app permissions:
    `test_maintainer_right_read_needs_metadata_on_the_repository_token`,
    `test_app_rights_are_the_rights_the_train_asks_for`,
    `test_switched_off_asks_for_read_rights_only`.
  - Refusals, bounded reasons and rechecking before landing:
    `test_maintainer_right_refusal_holds_bot_pull_request`,
    `test_maintainer_right_refusal_reason_stays_bounded`,
    `test_maintainer_right_refusal_disarms_then_next_run_lands`,
    `test_landing_reads_the_review_bodies_again`.
