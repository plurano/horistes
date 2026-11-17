<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0088: A push trigger counts only for the default branch

- **Status**: Accepted
- **Date**: 2026-11-16
- **Authors**: Lusoris

## Context

A repository that runs release-please itself keeps release creation out of the
merge train, as [ADR-0040](0040-the-merge-train-leaves-a-release-to-its-writer.md)
requires. Issue 196 identifies a gap in `workflow_has_allowed_trigger` at
`9e62c42` in
[merge-train.yml](../../.github/workflows/merge-train.yml): it accepts a block
`push:` event without reading its branch or tag filters. A workflow confined to
another branch or to tags therefore makes the train skip a default-branch
release. `test_planted_push_filter_allows_train_release` in
[test_release_labels.py](../../tests/test_release_labels.py) reproduces those
exclusions against the original module.

## Decision

`reads_own_release` reads the repository's `default_branch` through one repository
metadata REST request after finding workflow files. Empty workflow lists and a
missing workflows directory require no metadata request. A failed request or a
missing, empty or non-string `default_branch` fails the settings step without
outputs. `test_workflow_files_read_repository_once`,
`test_no_workflow_files_reads_no_repository`,
`test_invalid_default_branch_fails_without_outputs`,
`test_absent_default_branch_fails_without_outputs` and
`test_failed_repository_read_fails_without_outputs` check these boundaries.

The trigger reader evaluates block push filters against that default branch.
Branch inclusions follow pattern order, including negative patterns; the last
match wins. Branch exclusions reject any matching pattern. A tag filter without
a branch filter excludes branch pushes. Paths and unrelated keys do not affect
this decision. Scalar, flow-list and block-sequence filter values, including
compact sequences, are covered by `test_push_filter_values_and_bounds` and the
planted exclusion cases. The semantics follow
[GitHub's workflow syntax reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushbranchestagsbranches-ignoretags-ignore),
as read on 2026-11-16.

The supported patterns contain letters, digits, `.`, `_`, `/`, `-` and stars,
with one optional leading `!` for branch inclusions. Anchored regular expressions
escape literal characters; `*` stops at `/` and `**` crosses it. Evaluation is
bounded to 200 patterns and the existing 5000-line workflow window. Unsupported
characters, ref-prefixed patterns, YAML anchors, aliases, tags, block scalars,
non-scalar children and combined branch inclusion/exclusion filters keep counting.
Inline event mappings also keep their existing answer. These controls live in
`test_undecidable_push_filter_keeps_counting` and
`test_push_trigger_controls_skip_train_release`.

An undecidable filter retains today's skipped train release: creating a second
release is the failure ADR-0040 removed. Other allowed triggers and the
trigger-blind local workflow call graph retain their existing behavior, checked
by the trigger controls and `test_local_call_of_release_workflow_skips_release`
in the same test file.

## Consequences

- The eight cases in `test_planted_push_filter_allows_train_release` permit the
  train's release step when the repository's push trigger excludes its default
  branch. `test_push_filter_mutations_flip_planted_oracle` rejects seven mutations
  of the filter decisions, matching and default-branch propagation.
- Release settings perform one extra REST read per run that examines a non-empty
  workflow list. The repository-read tests above check both the count and failure
  behavior; config settings that already skip release creation still short-circuit.
- The release-labels step remains below the 4096-field ceiling from
  [ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md) and
  [ADR-0082](0082-the-train-graphql-documents-leave-the-rules-step.md). The step grows from
  2799 to 3335 whitespace-separated fields (536 added); the train-rules step
  remains unchanged at 3345 fields in the same workflow.
