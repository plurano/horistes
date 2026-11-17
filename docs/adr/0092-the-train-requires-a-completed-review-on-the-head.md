<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0092: The train requires a completed review on the head

- **Status**: Accepted
- **Date**: 2026-11-17
- **Authors**: Lusoris

## Context

This record amends [ADR-0090](0090-the-train-holds-on-an-incomplete-review.md).
An absence of recognized limit notices did not prove that a reviewer had completed
its review. The reader also compared timestamp strings, discarded review state,
and allowed unrelated authors' tables to clear a reviewer's limit notice.
Sticky summaries can be edited after a push without changing their creation time.

The canonical implementations are [tools/review_state.py](../../tools/review_state.py)
and [tools/merge_train.py](../../tools/merge_train.py). Their regression inputs live
in [tests/test_review_state.py](../../tests/test_review_state.py) and
[tests/test_merge_train.py](../../tests/test_merge_train.py).

## Decision

A. The optional `review_bots` workflow input lists comma-separated logins. The
train lowercases them and removes a `[bot]` suffix, rejecting malformed logins.
At least one listed reviewer must supply a completed formal review with
`commit.oid` equal to the head, or a summary containing exactly one reviewed
range ending at the full head. Only COMMENTED, APPROVED, and CHANGES_REQUESTED
formal reviews qualify. The PR author cannot supply its own review evidence.
`test_adr92_head_evidence`, `test_adr92_zero_review`,
`test_adr92_allowance_head_evidence`, `test_adr92_invalid_review_bots`, and
`test_adr92_zero_review_train` cover admission and the served train.

The default is empty: zero-review admission remains off because a repository
without a review bot would otherwise never land. Every sweep names this state
once, checked by `test_adr92_zero_review_off_summary`. Existing incomplete-review
and unanswered-finding holds still apply. Revisit when every repository on the
fleet train has a review bot: default to that reviewer then.

B. Notice recognition remains anchored to notice lines. It includes Sourcery's
own-budget notice, headings with at most four symbols before the notice, and the
explicit repository, organization, and commit qualifiers. Ordinary prose and
table cells remain outside the notice grammar. `test_adr92_reader_planted` cases
`B-*` and `test_adr92_notice_controls` cover these boundaries.

C. The query selects `updatedAt` on comments and reviews and `commit { oid }` on
reviews. Body time is the latest parsed creation/submission or edit instant.
`C-*` cases in `test_adr92_reader_planted` cover edited notices and completions;
`test_review_bodies_query_fields` and
`test_review_bodies_query_supplies_incomplete_reader` bind the query to the reader.

D. One evidence reader serves both admission and notice clearing. Evidence is
scoped to the same non-author login. A formal completed review or a body with
pre-merge rows can clear an earlier ordinary notice; bodies with non-allowance
notices and PENDING or DISMISSED reviews cannot. Their notices still hold.
A notice naming the head requires head evidence even when timestamps suggest
completion. `D-*` cases in `test_adr92_reader_planted` cover author tables,
other reviewers, stale tables, dismissed reviews, and formal completion.

E. Times are aware UTC instants parsed with `datetime.fromisoformat`. Missing,
malformed, or naive timestamps are unknown. Any unknown commit time, or no
commits, makes the latest commit unknown. Unknown ordering holds. A notice at
the commit instant holds; one second earlier does not, unless it names the
head. Exhausted allowances require reviewed-head coverage or later same-login
head evidence. `E-*` cases in `test_adr92_reader_planted` cover these rules.

F. Unreadable or truncated connections hold before either review reader runs.
Non-list `nodes` never raise in the bounded reader. Missing detail head uses
the primary pull head; a different detail head holds as moved.
`test_adr92_nodes_malformed`, `test_adr92_malformed_connections`,
`test_adr92_moved_head`, and `test_adr92_missing_detail_head` plant these cases.

G. Reader functions stay within 60 lines and McCabe 10. Inner scans use scalar
bounds, including a separate markdown-cell bound. `test_adr92_reader_complexity_bounds`
checks the line and McCabe ceilings; the source documents the cell bound.

H. `test_completed_review_clears_incomplete_hold` first asserts a hold on the
same detail, then adds each reviewer's head evidence and asserts clearance.
`test_adr92_notice_boundary_without_evidence` isolates equal-time and one-second
boundaries without adding evidence.

I. Immediately before arming an eligible BEHIND pull request, the train re-reads
its reviews. A late notice holds without arming or updating. Admission reads
and arming reads share `REVIEW_READS`; a spent budget holds arming unread.
The existing final landing read remains separately bounded by the landing path.
Bot dependency updates retain their exemption. `test_adr92_arming_rereads` and
`test_adr92_arming_read_budget` cover late notices, clean reads, and exhaustion.

## Consequences

Configured repositories hold zero-review heads with `review body: review incomplete:
no completed review by <logins> on head <head>`. Unknown input holds instead of
proving completion. A bot's later review cannot clear another bot's notice.
A current-head review may still hold for unanswered findings under ADR-0070.

`test_adr92_mutations_reject_defects` restores each A–F and I defect in isolated
module copies and requires a planted failure after an unmutated pass.
Run the two named regression files with `.venv/bin/python -m pytest -p no:cacheprovider
tests/test_review_state.py tests/test_merge_train.py` and
`PYTHONDONTWRITEBYTECODE=1` for mutation replay.
