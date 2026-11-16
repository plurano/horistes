<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0090: The train holds on an incomplete review

- **Status**: Accepted
- **Date**: 2026-11-16
- **Authors**: Lusoris

## Context

VMAFx/vmafx pull request 3507, authored by a person, had no formal reviews.
Its last commit was at 2026-11-16T16:10:16Z; CodeRabbit posted a review-limit
notice at 16:40:51Z. The train landed it at CLEAN at 17:14:57Z. The captured
[payload](../../tests/fixtures/review-state/pr3507-review-state.json) and
[notice](../../tests/fixtures/review-state/pr3507-coderabbit-6922706444.md)
replay the incomplete-review verdict in `test_review_incomplete_pr3507_replay`
in [tests/test_review_state.py](../../tests/test_review_state.py).

The fleet review gate reported `review incomplete: coderabbitai limit reached
at 2026-11-16T16:40:51Z`. The fleet's manual landing path refuses that state;
the train was the path that admitted this pull request. Its
[ADR-0070](0070-the-train-holds-on-unanswered-review-bodies.md) body check
looked only for findings in bot reviews, so zero reviews produced no hold.
`test_planted_incomplete_review_body_hold` and
`test_planted_incomplete_review_train_holds` in
[tests/test_merge_train.py](../../tests/test_merge_train.py) preserve this
failure as a regression case.

## Decision

The train holds on an incomplete review using
[tools/review_state.py](../../tools/review_state.py), a verbatim extract of
the fleet planning gate's reader. Horistes is its canonical home; later gate
changes come here by pull request. `review_body_hold` in
[tools/merge_train.py](../../tools/merge_train.py) runs this reader after the
connection-count guards and before unanswered findings, preserving one hold
reason with the existing `review body:` prefix.

The pull request author is the only answerer; `test_planted_incomplete_review_body_hold`
checks that a bot author in the detail response cannot replace that identity.
Eligibility remains that of ADR-0070, excluding bot dependency updates through
`review_body_eligible` and `late_review_hold` in `tools/merge_train.py`.
The workflow's [sparse checkout](../../.github/workflows/merge-train.yml)
contains both modules from the same workflow commit, checked by
`test_train_script_comes_from_the_workflow_commit` and
`test_sparse_checkout_missing_review_state_is_refused`.

A completed review covering the head clears the hold under the reader's
existing rules. `test_completed_review_clears_incomplete_hold` exercises a
later CodeRabbit summary with a pre-merge checks table and a notice older
than the last commit. The allowance and paused-review cases in
`test_review_incomplete_gate_cases` check their respective coverage evidence.

## Consequences

The author sees `review body: review incomplete: <login> limit reached at <ts>`
in the train summary and can ask the bot to review again. An armed pull request
is disarmed with the same reason; both outcomes are checked by
`test_planted_incomplete_review_train_holds` in `tests/test_merge_train.py`.

`tools/review_state.py` is the single canonical reader. The fleet gate is to
import it later from a pinned Horistes commit, and subsequent reader changes
arrive by pull request, as the module's provenance docstring records. The
ported cases in `tests/test_review_state.py` retain limit, ordering, pause,
promotion, allowance and reviewed-range boundaries. Run them with
`.venv/bin/python -m pytest -p no:cacheprovider tests/test_review_state.py`;
train admission and checkout coverage run with
`.venv/bin/python -m pytest -p no:cacheprovider tests/test_merge_train.py`.
