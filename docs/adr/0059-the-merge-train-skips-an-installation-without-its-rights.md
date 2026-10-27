<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0059: The merge train skips an installation that has not accepted its rights

- **Status**: Accepted
- **Date**: 2026-10-27
- **Authors**: Lusoris

## Context

When the app asks for a new right, every account that installed it has to accept the
request before the forge mints a token with that right. Until then the token request
for that installation answers HTTP 422 "The permissions requested are not granted to
this installation." On 2026-10-27 two accounts had not accepted `issues: write` and
`actions: read`. The sweep asked once per repository of such an installation, logged
one identical refusal per repository and did not say that the account, not the
repository, was the cause.

## Decision

- `mint` in `tools/merge_train.py` raises `UngrantedError` for that HTTP 422 answer.
- The sweep stops at the first such refusal in an installation. It names the account
  and the installation id with the number of repositories skipped, once, in the log
  and in the job summary, and counts those repositories as refused.
- Every other installation runs as before, and the run still exits 1 at the end, so a
  pending approval stays visible as a red run.

## Consequences

One refusal per installation instead of one per repository, and no further token
requests against an installation that cannot be served. Any other refusal still stays
per repository.

Tests: `test_installation_without_the_rights_is_skipped_and_the_others_run` plants an
installation answering 422; the other installations land and update and the run exits 1.
`test_without_the_installation_skip_each_repository_asks_again` removes the skip and sees
one token request per repository again.
