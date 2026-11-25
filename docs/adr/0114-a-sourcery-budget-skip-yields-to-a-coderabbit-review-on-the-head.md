<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0114: A Sourcery budget skip yields to a CodeRabbit review on the head

- **Status**: Accepted
- **Date**: 2026-11-22
- **Authors**: Lusoris

## Context

`LIMIT_NOTICE_RE` in [tools/review_state.py](../../tools/review_state.py) matches a
Sourcery budget line (`Sorry @…, you've used your own review budget…`). When such a
notice is newer than the last commit, `_notice_holds` keeps the train waiting and
`review_incomplete` reports `review incomplete: <login> limit reached at <instant>`.
The hold line may end with `; summary shape unmatched: <names>` as ADR-0092
section E describes.
`review_body_hold` in [tools/merge_train.py](../../tools/merge_train.py) prefixes that
with `review body:`.

On a clean pull request whose head already has a completed CodeRabbit review, forge
check runs can still show Sourcery as `COMPLETED` / `SKIPPED` with the same budget
summary on the head commit, while an issue comment repeats the budget sentence after
the last push. The train then held for days although CodeRabbit had finished on that
head.

GraphQL on the head returns a `coderabbit` check suite (app id 347564, slug
`coderabbitai`) with a `CodeRabbit` run `SUCCESS` and summary beginning
`Result: Success - Review completed`, and a `sourcery` suite (app id 48477, slug
`sourcery-ai`) with a `Sourcery review` run `SKIPPED` and a budget first line matching
the notice. Fixture summaries omit the live upgrade link; production text carries a
`scope=` query on that link.

## Decision

When a notice comment would hold only because of Sourcery budget text after the last
commit, the train does not hold if all of the following are provable from the pull
request node (including `headChecks` read through `review_incomplete`):

- the author login is `sourcery-ai`;
- every `LIMIT_NOTICE_RE` match in the body is the `budget` alternative;
- `headChecks` reads the latest pull request commit (`commits(last: 1)`), which must be
  exactly one readable node whose `oid` is the head; its `totalCount` counts every pull
  request commit, so any count of at least 1 is accepted for that page, while suite and
  run connections must still return every counted node;
- CodeRabbit finished on that head either through existing `review_evidence` for login
  `coderabbitai` with `on_head`, or through a readable `headChecks.coderabbit` suite
  whose app is id 347564 / slug `coderabbitai` and that holds a `CodeRabbit` run with
  status `COMPLETED`, conclusion `SUCCESS`, and summary first line exactly
  `Result: Success - Review completed`;
- Sourcery on that head is only budget skip: every `Sourcery review` run under a suite
  with app id 48477 / slug `sourcery-ai` has status `COMPLETED`, conclusion `SKIPPED`,
  and a summary whose first line matches the `budget` alternative, with at least one
  such run present.

Missing data, wrong shapes, truncated `totalCount`, or any wording deviation from the
allow-listed names, ids, and summary lines fails closed: the notice holds as before.
`REVIEW_BODIES` gains the `headChecks` alias; unreadable `headChecks` only withholds
this exemption and does not add a connection hold.

## Consequences

Any change by CodeRabbit or Sourcery to check-run names, app ids, or the success or
budget summary lines fails closed and the prior hold returns until the allow-list is
updated. Unresolved review threads and unanswered bot findings still hold through the
ruleset and `review_body_hold` paths unchanged.

Planted cases `test_adr114_p1_pass` through `test_adr114_h5_fail_closed_holds` in
[tests/test_review_state.py](../../tests/test_review_state.py) and
`test_adr114_h2_open_findings_hold` in [tests/test_merge_train.py](../../tests/test_merge_train.py)
replay both directions.

## Alternatives rejected

- **Wait for the budget reset:** leaves clean pull requests on the train for days
  while CodeRabbit has already reviewed the head.
- **Drop Sourcery limit notices entirely:** removes detection of genuine Sourcery skip
  states that are not paired with CodeRabbit completion on the head.

## Revisit triggers

- Either bot changes its check-run name, GitHub app id, or the first summary line the
  train matches.
- The Sourcery plan or per-repository review budget changes materially.
- A second required bot reviewer is added alongside CodeRabbit and Sourcery.