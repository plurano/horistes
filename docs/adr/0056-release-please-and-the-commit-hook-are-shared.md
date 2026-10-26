<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0056: Release-please and the commit hook are shared

- **Status**: Accepted
- **Date**: 2026-10-26
- **Authors**: Lusoris

## Context

Two consumer repositories, `lusoris/SnatchArr` and
`lusoris/jellyfin-plugin-snatcharr`, carry byte-identical copies of two
pieces. A deduplication scan across the fleet (`praetorctl dedupe scan
--across`) found them as `DX-3ef96d6491-github-workflows-1` and
`DX-3ef96d6491-config-hooks-1`:

- `.github/workflows/release-please.yml`, which runs release-please on a push
  to the default branch with a token minted by a GitHub App, so that release
  pull requests and releases start workflows (with GITHUB_TOKEN they start
  none, [ADR-0031](0031-the-merge-train-app-runs-release-please.md)).
- `.config/hooks/commit-msg.sh` and its test `commit-msg_test.sh`, which refuse
  a commit message without a Conventional Commit subject (summary of at most
  72 characters) or a Signed-off-by trailer, and one whose trailer or
  "generated with" footer names a model or coding tool.

A workflow can be called by commit. A Git hook cannot: it runs on the
developer's machine before the commit exists, so the consumer has to keep a
copy of the file.

## Decision

- `.github/workflows/release-please.yml` is a reusable workflow taken from
  `lusoris/SnatchArr` at commit `3726ca1d7286` (EUPL-1.2, same author). The
  values that differed per repository are inputs: `runner`, `app_client_id`,
  `config_file`, `manifest_file` and the secret `app_private_key`. The job has
  no rights of its own; the app token carries them. No `release-type` input is
  passed, since the action then ignores the configuration file.
- `hooks/commit-msg.sh` and `hooks/commit-msg_test.sh` are the SnatchArr files
  at the same commit; only the copyright holder line changed (`Lusoris`, the
  fleet's holder line).
- `.github/workflows/hook-copy.yml` checks the copy. It checks out `hooks/` and
  `tools/hook_copy.py` from its own commit (`job.workflow_repository` at
  `job.workflow_sha`, as in [ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md)),
  so the commit a caller pins names both the check and the file it compares
  against. `tools/hook_copy.py` compares the SHA-256 of each file and fails on
  a difference, a missing copy or a symbolic link, printing both digests. The
  copy's own test then runs against the copy.
- Each workflow has a caller template in `templates/callers/`. The hook copy
  caller reports the required check `Hook copy` through a result job
  ([ADR-0030](0030-a-result-job-reports-the-calling-jobs-name.md)); the
  release-please caller runs on push only and is no check of a pull request.

## Consequences

- **Positive**:
  - The two SnatchArr repositories drop their own release-please steps and
    call the shared workflow by commit.
  - A copy edited in place, or left behind after a repin, fails `Hook copy`
    with both digests in the log; the copy is refreshed by copying the files
    at the newly pinned commit.
- **Negative**:
  - The present SnatchArr copies differ in the copyright holder line, so each
    repository replaces its copy with the horistes file when it switches.
  - A repository that the merge-train sweep already serves with release-please
    ([ADR-0031](0031-the-merge-train-app-runs-release-please.md)) and that also
    calls this workflow has two writers on its release branch. It uses one of
    the two.
  - `.github/actionlint.yaml` accepts `job.workflow_*` for `hook-copy.yml`
    until actionlint knows the properties; the entry expires like the others.
- **Neutral**: `tests/test_commit_messages.py` keeps checking this
  repository's own commits in CI; the hook does not replace it.
