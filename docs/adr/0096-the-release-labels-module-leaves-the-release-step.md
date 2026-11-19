<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0096: The release labels module leaves the release step

- **Status**: Accepted (amends [ADR-0094](0094-the-review-settle-rule-leaves-the-rules-step.md))
- **Date**: 2026-11-19
- **Authors**: Lusoris

## Context

The engine caps each workflow run script at 4096 fields
([ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md)). After the main
merge, the release job's inline labels module in
[merge-train.yml](../../.github/workflows/merge-train.yml) counts 4156 under
the conservative 4000-field guard introduced by
[ADR-0094](0094-the-review-settle-rule-leaves-the-rules-step.md). The measured
engine count is 4057, leaving only 39 fields below its cap. The earlier
3530-field conservative count predates the default-branch push filter in
[ADR-0088](0088-a-push-trigger-counts-only-for-the-default-branch.md).

The labels module implements the release settings and label handling from
[ADR-0031](0031-the-merge-train-app-runs-release-please.md) and
[ADR-0040](0040-the-merge-train-leaves-a-release-to-its-writer.md). Further
changes should not require compacting that logic to fit a workflow step.

## Decision

Move the heredoc body into
[tools/release_labels.py](../../tools/release_labels.py), adding only its SPDX
header. Replace the writing step with the same pinned checkout action used by
the train sweep (ADR-0053). Read the workflow's repository and commit through
the job context, retain no credentials, and sparsely check out only the module
into `merge-train-source`, the directory name the train job uses, with cone
mode disabled. The release job keeps its empty token permissions; its workflow
repository is public.

Both the settings reader and the label mover run the module beneath
`$GITHUB_WORKSPACE/merge-train-source/tools/release_labels.py`. Their arguments,
environment, outputs and ordering stay unchanged. A caller pinned to a workflow
commit therefore runs that commit's labels module, as it does the train sweep.

## Consequences

Behaviour stays unchanged: the original heredoc and the new file have equal
ASTs, and the body beneath the SPDX header is byte-identical. The existing
[release-label tests](../../tests/test_release_labels.py) run the checked-out
file, so coverage measures it at 91.53%.

Release step 2 is a checkout, and the release job's largest remaining run step
counts 3 conservative fields. The unchanged
[run-script guard](../../tests/test_run_script_fields.py) passes every workflow
step; the largest merge-train run script remains the train rules at 3424
fields. These are conservative counts, not a new engine audit result.

## Tests

[tests/test_release_labels.py](../../tests/test_release_labels.py) checks the
checkout's action pin, workflow repository and commit, exact sparse list,
destination, disabled credential persistence and cone mode, empty job
permissions, and checkout ordering before both exact run commands. Three
temporary workflow mutations remove the module from the sparse list, replace
the workflow commit with a branch, or restore a temporary-directory run path;
each is rejected. The checkout check also rejects the original inline workflow.
The module's Ruff check remains in this test file.

[tests/test_merge_train.py](../../tests/test_merge_train.py) still selects one
settings step and one label-judging step by their existing filename suffixes.
Its release-job assertions and
[tests/test_review_state.py](../../tests/test_review_state.py) retain their
existing behaviour coverage.

Run the focused checks with `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m
pytest -p no:cacheprovider tests/test_release_labels.py
tests/test_run_script_fields.py tests/test_merge_train.py tests/test_review_state.py`.
