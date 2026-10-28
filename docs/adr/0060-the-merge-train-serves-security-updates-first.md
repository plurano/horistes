<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0060: The merge train serves security updates first

- **Status**: Accepted
- **Date**: 2026-10-28
- **Authors**: Lusoris

## Context

A run makes at most one write per repository, and every step picked the oldest open
pull request. On 2026-10-28 a Renovate security update in golusoris/golusoris waited
behind an older Renovate update of a digest that Renovate re-pushes on every push to
the default branch: two runs in a row committed the regenerated files to the digest
update, and the security update got none. The order let a pull request starve in four
places:

- the generated-files write went to the oldest candidate with changed files, and a
  re-pushed head always had changed files again;
- landing took the oldest CLEAN pull request;
- the refresh of a draft-stopped run took the oldest red pull requests, at most three;
- updating took the oldest BEHIND pull request, armed ones first.

## Decision

- `is_security` in the train rules decides a security update: a label in
  `security_labels`, or "[SECURITY]" in the title. The minimum-age skip uses the same
  test.
- `serving_order` puts security updates first and keeps the oldest-first order within
  each group. Landing, the in-flight hold, the refresh and the update read the rows in
  that order; the job summary keeps the query's order.
- Updating puts a security update before an armed one, and keeps armed before unarmed
  within each group.
- The generated-files write goes to the candidate whose head commit is oldest
  (`committedDate`, read with the check suites), security updates first. A head the
  update tool or the train pushed since is served after one never served.

## Consequences

A security update no longer waits for an older pull request in any step. Pull requests
whose heads keep moving share the generated-files write in turn instead of keeping it.
Two security updates are still served oldest first.

Tests: `test_security_update_gets_the_generated_files_write_before_an_older_digest`
plants the golusoris case; with the old order the digest update gets the write.
`test_security_title_counts_like_the_label_for_the_generated_files_write`,
`test_generated_files_write_serves_the_longest_waiting_head_first`,
`test_security_pull_request_lands_before_an_older_clean_one` and
`test_security_pull_request_is_updated_before_an_older_armed_one` cover the other
paths; all five fail without the change.
