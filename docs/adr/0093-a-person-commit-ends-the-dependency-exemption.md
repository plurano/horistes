<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0093: A person commit ends the dependency exemption

- **Status**: Accepted
- **Date**: 2026-11-17
- **Authors**: Lusoris

## Context

The dependency exemption in [ADR-0067](0067-the-train-settles-reviews-before-landing.md)
and [ADR-0070](0070-the-train-holds-on-unanswered-review-bodies.md) used the branch
prefix and PR author. That author remains a bot after a person adds commits.
The bot-account rule in [ADR-0081](0081-a-person-review-answers-an-app-pull-request.md)
recognizes both GraphQL bot accounts and logins ending in `[bot]`; commit users
need the latter because their selected author shape contains only a login.

A live dependency update merged with five commits: one by Renovate and four by
a person. CodeRabbit's summary contained a paused block whose last reviewed
commit differed from the head. The summary was edited after the newest commit.
The incomplete-review reader would have held it, but the train exempted the PR
before consulting that reader. Other histories contained bot commits after
person commits, so inspecting only the head would preserve the bypass.

Separately, a paused notice edited before a later push could expire under the
instant-ordering rule in [ADR-0090](0090-the-train-holds-on-an-incomplete-review.md)
and [ADR-0092](0092-the-train-requires-a-completed-review-on-the-head.md).
A paused reviewer does not automatically review that pushed head.
The anonymized replay is
[dependency-person-commits-paused.json](../../tests/fixtures/review-state/dependency-person-commits-paused.json).

## Decision

A. A dependency PR retains the exemption only when the PR author and every
commit author are bots. The list query reads at most 50 commit author nodes per
pull request. Missing, malformed, empty, or truncated author connections lose
the exemption and receive the full review rules. The named bound and query
literal must agree. The `is_bot_dependency` predicate now lives in
[tools/review_state.py](../../tools/review_state.py)
([ADR-0094](0094-the-review-settle-rule-leaves-the-rules-step.md)) and serves
review settling, admission, and the final review read in
[tools/merge_train.py](../../tools/merge_train.py).

`test_adr93_dependency_paused_train`, `test_adr93_bot_commit_authors`,
`test_adr93_unreadable_authors`, and `test_adr93_author_query_bound` in
[tests/test_merge_train.py](../../tests/test_merge_train.py) cover the live shape,
an interior person commit, absent users, malformed reads, truncation, and the
50-commit boundary. All-bot updates still land without fetching review bodies.

B. A paused notice persists across later pushes regardless of its edit instant.
It clears when its existing paused block proves the head reviewed, or when the
same reviewer supplies later completed head evidence after the push: a completed
formal review on the full head, an exact reviewed range ending at that head, or
a later pause summary naming the full head as last reviewed commit. A pause
summary must carry only pause notices and unambiguous block-local head evidence.
A later review on an older commit, another reviewer's evidence, or a generic
pre-merge table cannot clear it. The canonical reader remains
[tools/review_state.py](../../tools/review_state.py);
`test_adr93_paused_notice` in
[tests/test_review_state.py](../../tests/test_review_state.py) covers these cases.
Its `second-summary-proves-head` case covers clearance by a later pause summary;
`second-summary-other-reviewer`, `second-summary-before-push`,
`second-summary-short-head`, `second-summary-mixed-notice`, and
`second-summary-missing-last-reviewed` keep reviewer scope, ordering, full-head
proof, notice type, and the last-reviewed field mandatory.

## Consequences

Each list page adds at most 25 pulls × 50 commit author nodes. Updates beyond
the bound receive full review rather than an exemption based on partial history.
Pure bot updates retain the exemption because CodeRabbit frequently auto-pauses
repeatedly rebased updates; holding those pauses would stall every dependency
update. A person's contribution instead makes completed head review necessary
when a pause is present.

`test_adr93_mutations_reject_defects` restores three defects in temporary copies:
removing author checks, checking only the last author, and expiring a pause
after a push. Each mutation must fail its named regression after an unmutated
pass. Replay with `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p
no:cacheprovider tests/test_review_state.py tests/test_merge_train.py`.
