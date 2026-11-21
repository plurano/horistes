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
[tools/release_labels.py](../../tools/release_labels.py) and its workflow parser
[tools/release_workflows.py](../../tools/release_workflows.py), each with an SPDX
header. Replace the writing step with the same pinned checkout action used by
the train sweep (ADR-0053). Read the workflow's repository and commit through
the job context, retain no credentials, and sparsely check out the two modules
into `merge-train-source`, the directory name the train job uses, with cone
mode disabled. This narrow checkout requires Git 2.28 or later on the runner
PATH: the pinned checkout action sets that minimum in
[`src/git-command-manager.ts` lines 16 and 730–733](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/src/git-command-manager.ts#L730-L733).
With an older Git, the manager error falls back to the REST API when LFS is off
([`src/git-source-provider.ts` lines 383–390](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/src/git-source-provider.ts#L383-L390)),
which downloads the whole repository archive and uses `tar` to extract it off
Windows ([`src/github-api-helper.ts` lines 53–56](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/src/github-api-helper.ts#L53-L56)).
The old heredoc needed neither Git nor `tar`. The release job keeps its empty
token permissions; its workflow repository is public.

Both the settings reader and the label mover run the module beneath
`$GITHUB_WORKSPACE/merge-train-source/tools/release_labels.py`. Their arguments,
environment, outputs and ordering stay unchanged. A caller pinned to a workflow
commit therefore runs that commit's labels module, as it does the train sweep.

## Consequences

The heredoc body first moved byte-identical beneath the SPDX header, with an
equal AST. It is now split into two modules to stay below Codacy's
file-size limit; parser bodies are unchanged apart from renaming a parse word.
The URL allow-list is the one behaviour change: requests and redirect targets
must use lower-case `https`, or lower-case `http` with host `127.0.0.1`,
`localhost` or `::1`. Other HTTP hosts, `file`, `ftp`, `data`, upper-case schemes
and URLs without a parseable host are refused before opening them. Initial calls
and redirects have independent checks and distinct refusal diagnostics.
The existing [release-label tests](../../tests/test_release_labels.py) run the
checked-out files, so coverage measures `release_labels.py` at 90.83%
and `release_workflows.py` at 92.79%.

Release step 2 is a checkout, and the release job's largest remaining run step
counts 3 conservative fields. The unchanged
[run-script guard](../../tests/test_run_script_fields.py) passes every workflow
step; the largest merge-train run script remains the train rules at 3424
fields. These are conservative counts, not a new engine audit result.

## Tests

[tests/test_release_labels.py](../../tests/test_release_labels.py) checks the
checkout's action pin, workflow repository and commit, exact sparse list,
destination, disabled credential persistence and cone mode, empty job
permissions, both exact run commands, and the order checkout < settings <
release < after. Four temporary workflow mutations remove a module from the
sparse list, replace the workflow commit with a branch, restore a
temporary-directory run path, or move after before release; each is rejected.
The checkout check also rejects the original inline workflow.
The Ruff check covers both modules.

`test_settings_refuses_file_url_before_read` plants a canary and checks the
call refusal, no leaked content and no stand-in request.
`test_settings_refuses_ftp_url_before_connection` rejects a non-allowed scheme
on loopback; `test_release_url_allow_list` checks accepted and refused URLs
without connecting. `test_settings_refuses_redirect_url` plants forbidden
redirects, including loopback FTP, and checks the redirect refusal.
`test_settings_follows_allowed_redirect` retains both relative and absolute
redirects to the allowed stand-in.
`test_planted_release_after_before_release_is_refused` rejects reordered steps.

[tests/test_merge_train.py](../../tests/test_merge_train.py) still selects one
settings step and one label-judging step by their existing filename suffixes.
Its release-job assertions and
[tests/test_review_state.py](../../tests/test_review_state.py) retain their
existing behaviour coverage.

Run the focused checks with `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m
pytest -p no:cacheprovider tests/test_release_labels.py
tests/test_run_script_fields.py tests/test_merge_train.py tests/test_review_state.py`.
