<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0094: The review-settle rule leaves the rules step

- **Status**: Accepted (amends [ADR-0082](0082-the-train-graphql-documents-leave-the-rules-step.md))
- **Date**: 2026-11-17
- **Authors**: Lusoris

## Context

The engine caps a workflow run script at 4096 fields
([ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md)). The measured
`Write the train rules module` step in
[.github/workflows/merge-train.yml](../../.github/workflows/merge-train.yml)
counted 4073 fields before
[ADR-0093](0093-a-person-commit-ends-the-dependency-exemption.md), and 4237
with it. The engine refused the latter. Its count drops comments, joins
backslash-newlines, counts other newlines as fields, splits words on whitespace,
and counts a word ending in a semicolon as two fields. The block from
`_is_bot_author` through `review_settle` accounts for 780 fields; removing that
block alone leaves 3457.

[ADR-0082](0082-the-train-graphql-documents-leave-the-rules-step.md) calls for
the next large rule to leave the step. Its suggested destination,
[tools/merge_train.py](../../tools/merge_train.py), imports `train_rules`, so
importing it back from the rules would introduce a cycle.
[tools/review_state.py](../../tools/review_state.py) already accompanies the
sweep in the sparse checkout and imports neither module. The settle semantics
from [ADR-0081](0081-a-person-review-answers-an-app-pull-request.md) and the
dependency author checks from ADR-0093 belong with that review reader.

## Decision

Move `_is_bot_author`, `BOT_COMMIT_READS`, `_commit_user`, `is_bot_dependency`,
`_responds`, `_standing_change_request`, `_on_head_reviews`,
`_review_timing_hold`, and `review_settle` into
[tools/review_state.py](../../tools/review_state.py). Move their private
dependencies `_REVIEW_HOLD`, `_thread_replies_only`, and `_person_reply` too.
Use the destination's existing `datetime` import; no second implementation or
bot-login predicate is needed.

The rules module imports `review_settle` and explicitly re-exports
`is_bot_dependency` for its existing test interface. The sweep imports
`is_bot_dependency` directly from `review_state`, alongside its evidence reader.
Both modules thus depend on `review_state`, which depends on neither.
`QUERY`, `STATUS_QUERY`, their commit-author selections, and `pull_query` stay
in the rules module because `pull_query` selects the document (ADR-0082).
The sparse checkout stays unchanged.

## Consequences

Behaviour stays unchanged: the extracted definitions have identical ASTs, and
[tests/test_merge_train.py](../../tests/test_merge_train.py) continues to load
the rules through the workflow. Mutation replays select a temporary copy of
the moved reader; an explicit older source tree selects its own reader and
workflow together.

The new conservative count is 3417 fields for train step 1, leaving 583 fields
below the local 4000 limit and at least 679 below the engine cap. Release step
2 counts 3530. These are conservative counts, not a new engine audit result.
[tests/test_run_script_fields.py](../../tests/test_run_script_fields.py)
checks every workflow run step before the engine gate. It retains comments and
joined lines, so its word, newline, and trailing-semicolon count over-counts
the engine rule. The engine's own count remains the judge.

## Tests

[tests/test_run_script_fields.py](../../tests/test_run_script_fields.py)
identifies each workflow, job, and step; covers accepted scripts and the
4000/4001 boundary; and rejects a temporary train rules step padded by 600
words. Before extraction, the workflow guard rejects the rules step at 4303
conservative fields.

[tests/test_merge_train.py](../../tests/test_merge_train.py) retains all
behavioural and mutation assertions, including ADR-0093's A1 and A2 mutations
against temporary `review_state.py` copies and B1 against its pause reader.
[tests/test_review_state.py](../../tests/test_review_state.py) covers the
canonical evidence reader and its paused-review cases.

Run the focused checks with `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m
pytest -p no:cacheprovider tests/test_review_state.py tests/test_merge_train.py
tests/test_run_script_fields.py`.
