<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0082: The train's GraphQL documents leave the rules step

- **Status**: Accepted (amends
  [ADR-0039](0039-the-merge-train-closes-the-issues-its-merges-name.md))
- **Date**: 2026-11-13
- **Authors**: Lusoris

## Context

The `Write the train rules module` step in
[.github/workflows/merge-train.yml](../../.github/workflows/merge-train.yml)
writes the rules module from a heredoc. The engine audit caps one run script at
4096 fields ([ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md)). The
dispatcher measured 4022 fields on commit `4b84ddd` on 2026-11-13.
[ADR-0081](0081-a-person-review-answers-an-app-pull-request.md) adds review fields
to `QUERY` and settle helpers to the rules, bringing the step to 4339 fields.
The engine audit that
[.config/lefthook/engine.sh](../../.config/lefthook/engine.sh) runs before each
commit refuses that commit.

[ADR-0039](0039-the-merge-train-closes-the-issues-its-merges-name.md) put the
GraphQL documents in the rules module.
[ADR-0070](0070-the-train-holds-on-unanswered-review-bodies.md) already put new
GraphQL documents in [tools/merge_train.py](../../tools/merge_train.py).

## Decision

Move `MERGE_STATE`, `COMMITS`, `MERGED`, `MERGE`, `ENQUEUE`, `DEQUEUE`, `ARM`,
`DISARM`, `DRAFT`, `READY` and `COMMIT_MUTATION` unchanged into
[tools/merge_train.py](../../tools/merge_train.py), the only file that sends
them. `QUERY` and `STATUS_QUERY` stay in the rules module because `pull_query`
there selects one.

The dispatcher's 2026-11-13 measurement without these documents counts 3605
fields in the rules step, and 3922 with ADR-0081: 174 fields under the cap in
[ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md).

## Consequences

Behaviour stays unchanged: the strings and their calls in
[tools/merge_train.py](../../tools/merge_train.py) stay the same, pinned by
[tests/test_merge_train.py](../../tests/test_merge_train.py).
[ADR-0037](0037-the-merge-train-writes-the-squash-message.md),
[ADR-0039](0039-the-merge-train-closes-the-issues-its-merges-name.md) and
[ADR-0064](0064-an-armed-pull-request-is-re-armed-when-retitled.md) describe
the documents in the rules module; those documents now live in
[tools/merge_train.py](../../tools/merge_train.py).

A run script over the cap still fails the engine audit before commit through
[.config/lefthook/engine.sh](../../.config/lefthook/engine.sh) and in CI through
[.github/workflows/ci.yml](../../.github/workflows/ci.yml).

The rules step keeps a margin of only 174 fields with ADR-0081. The next large
rule should move to [tools/merge_train.py](../../tools/merge_train.py) too.

## Tests

`test_script_passes_the_python_linter` in
[tests/test_merge_train.py](../../tests/test_merge_train.py) checks both the
rules module and the sweep script. The whole file pins behaviour: its fake
forge answers by mutation name. Run it with
`.venv/bin/python -m pytest tests/test_merge_train.py -p no:cacheprovider`.
