<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0070: The train holds on unanswered review bodies

- **Status**: Accepted (amends [ADR-0067](0067-the-train-settles-reviews-before-landing.md))
- **Date**: 2026-11-09
- **Authors**: Lusoris

## Context

The merge train periodically sweeps every repository the app is installed on, landing
the oldest clean pull request ([ADR-0029](0029-a-forge-app-drives-the-merge-train.md)).
Inline review threads block landing through the forge's `mergeStateStatus`, and
`review_settle()` in `.github/workflows/merge-train.yml` holds when a submitted review
on the current head is newer than the author's last push or review reply
([ADR-0067](0067-the-train-settles-reviews-before-landing.md)). Neither mechanism
gates findings placed only in a bot review's top-level body (such as CodeRabbit nitpick
or outside-diff-range comments, Sourcery overall or human-review assessments, or Codacy
blocking overviews and unchecked test suggestions), especially when a later push moves
the head commit without answering an earlier review body.

In pull request 147 of this repository, the pull request reached `CLEAN` merge state and
merged while a CodeRabbit review body carried an unanswered outside-diff-range finding.
In addition, GraphQL returns bot logins without the `[bot]` suffix (for example
`renovate` with `__typename: "Bot"`), so the `endswith("[bot]")` check in
`review_settle()` from [ADR-0067](0067-the-train-settles-reviews-before-landing.md)
did not match live GraphQL pull request authors.

The rules heredoc in `.github/workflows/merge-train.yml` is near the 4096-field cap per
workflow `run:` script ([ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md)), so
new review-body parsing and GraphQL queries belong in `tools/merge_train.py`.

## Decision

- Amend [ADR-0067](0067-the-train-settles-reviews-before-landing.md) in
  `.github/workflows/merge-train.yml` (`train_rules.py`):
  - Request `author { login __typename }` on the pull request node in `QUERY`.
  - Add `is_bot_dependency(pull, conf)`, which returns `True` when `headRefName` starts
    with a configured `dependency` prefix and the pull request author has
    `__typename == "Bot"` or a `login` ending with `"[bot]"`.
  - Use `is_bot_dependency(pull, conf)` in `review_settle()` and export it for
    `tools/merge_train.py`.
- Add review-body hold evaluation in `tools/merge_train.py`:
  - Define the `REVIEW_BODIES` GraphQL query fetching `reviews(first: 100)`
    (`totalCount`, `databaseId`, `state`, `body`, `author { login __typename }`),
    `comments(first: 100)` (`totalCount`, `body`, `author { login }`), and
    `commits(first: 100)` (`totalCount`, `commit { message }`).
  - Add pure helper `review_body_hold(pull, detail)`:
    - Hold with `"review body: more than 100 <kind>, cannot read every answer"` when any
      of `reviews`, `comments`, or `commits` reports `totalCount > 100`.
    - Inspect candidate reviews on any commit of the pull request where
      `author.__typename == "Bot"` and `state` is in `{"COMMENTED", "CHANGES_REQUESTED", "APPROVED"}`
      (`DISMISSED` and `PENDING` are ignored).
    - Strip HTML comments (`<!--.*?-->`) and detect findings per bot login (after
      stripping any `"[bot]"` suffix; shapes taken from live reviews 2026-11-09):
      - `coderabbitai`: finding when any count in
        `(Nitpick|Outside diff range|Duplicate|Additional) comments` (`: N` or ` (N)`)
        is greater than zero, or when `[!CAUTION]` appears in the body; empty body has no
        finding; clean when body holds `Actionable comments posted:` or a `Review info`
        summary; otherwise fails closed as an unknown shape; the autofix checkbox is not
        a finding.
      - `sourcery-ai`: finding when any marker appears (case-insensitive:
        `high level feedback`, `## Overall Comments`, `security issue`,
        `Needs a human reviewer`, `Approval pending`), or when any linked-issue line
        `(\d+) of (\d+) met` has first < second; clean when body holds a known Sourcery
        opener or assessment (`Hey - I've found`, `Hey - I've reviewed your changes`,
        `### Sourcery assessment`); otherwise fails closed as an unknown shape.
      - `codacy-production`: finding when `No blocking issues noticed` is absent or any
        line matches `^- \[ \] `.
      - Any other bot: finding when the comment-stripped body is non-empty.
    - Treat a review with findings as answered when `pullrequestreview-<id>` or
      `review <id>` (case-insensitive, word-bounded) appears in an issue comment by the
      pull request author, a review body by the pull request author, or any commit
      message of the pull request.
    - Return `"review body: <login> review <id> has findings without a reply naming pullrequestreview-<id>"`
      (with `" (and N more)"` when multiple reviews remain unanswered), or `None`.
  - In `run()` in `tools/merge_train.py` (`hold_review_bodies()`), after computing
    initial `verdict()` rows and before building `notes`, fetch `REVIEW_BODIES` only for
    rows whose reason is in `("land", "behind", "red")` or that have `autoMergeRequest`
    set, and that are not `is_bot_dependency(pull, conf)`. Replace the row's reason with
    the hold reason when held; leave all other rows untouched without fetching.
  - Extend `disarm_red_scanning()` in `tools/merge_train.py` so an armed pull request
    held by a review body is disarmed via `DISARM` with note `"disarmed: <hold reason>"`,
    preserving `"disarmed: code-scanning check failed"` when code scanning is red.

## Consequences

- **Positive**:
  - Pull requests wait while any bot review body on any commit of the pull request
    carries an unanswered finding; authors unblock landing by replying in an issue
    comment or review body naming `pullrequestreview-<id>` (or `review <id>`), or by
    pushing a fix commit whose message names that review identifier.
  - Live GraphQL bot dependency pull requests (`__typename: "Bot"` without `[bot]`) are
    recognized consistently across `review_settle()` and `hold_review_bodies()`.
  - `REVIEW_BODIES` is fetched only for actionable or armed non-bot pull requests,
    preserving GraphQL rate budget on held, draft, or bot-dependency pull requests.
  - Coverage in `tests/test_merge_train.py` verifies live bot exemption, per-bot body
    classification, author comment/review/commit answers, pagination bounds, disarming
    of armed pull requests, and query field selection.
- **Negative**:
  - Race window on armed pull requests: if a pull request is already armed for forge
    auto-merge when a bot posts a late review body, the forge may merge the pull request
    before the next merge-train sweep runs and disarms it.
  - Unrecognized bot review body formats fail closed and require an explicit author
    reply or parser update.
  - Twelve of the thirteen Sourcery `Hey - I've found N issues` reviews in PRs 138 to
    153 end with `**Approval pending.**` and a `Blocking findings:` list; the thirteenth
    (review 5818404249 on PR 138) has the blocking list and a security-issue list. Each
    of the thirteen holds until the author names it, even when the same findings sit in
    inline threads the thread gate already covers. The body is Sourcery's own blocking
    verdict, so it counts as a finding; one author comment naming the review clears it.
