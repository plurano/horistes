<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0101: A failed read leaves the sweep green

- **Status**: Accepted
- **Date**: 2026-11-20
- **Authors**: Lusoris
- **Amends**: [ADR-0080](0080-the-train-outlasts-transient-forge-errors.md),
  [ADR-0070](0070-the-train-holds-on-unanswered-review-bodies.md)

## Context

The incident measurements supplied with issue 224 (2026-11-20, paraphrased)
describe a sweep that served every other repository and ran its release legs,
but ended red on two exhausted reads: a body cut at 219264 of 845638 bytes and
an HTTP 502 with an HTML gateway response. `sweep()` already continues after a
repository refusal, but `main()` returns 1 whenever any refusal remains.

The same measurements show a fleet repository with 95 open pull requests
answering the open-pull query with HTTP 502 after 11.4 seconds at 25 entries per
page. At 10 entries it returned 344324 bytes in 4.8 seconds; at 5 it returned
129747 bytes in 3.3 seconds. Another repository with 92 open pull requests
returned 215580 bytes in 8.2 seconds at 25 entries and 92823 bytes in 5.1 seconds
at 10. The source at adoption uses 25-entry pages and four pages; the payload
measurement suggests roughly 34 KB per pull request for the largest answer.
These are supplied host measurements, not a network experiment repeated here.

Sourcery also posts a COMMENTED review saying that the repository has no access
to its service and offering a billing upgrade. `_sourcery_finding()` treats an
unknown non-empty body as a finding, so this notice holds an otherwise clean
pull request despite containing no review finding.

## Decision

`UnreadError`, a subclass of `RefusedError`, identifies only a transient read
that still fails after `READ_ATTEMPTS` (3) attempts. `call()` raises it on the
last failed GET, and `read_graphql()` on the last failed GraphQL read. Both
preserve the underlying transport cause for `transient()`. Writes still run
once, and non-transient reads, including HTTP 500 and GraphQL errors, remain
refusals under ADR-0080.

`sweep()` records an unread repository or installation account separately from
refusals, preserves unsigned-bot notes, and continues. `summary()` lists each
target and the first line of its error under “Not read after 3 tries, the next
run reads them again:”. A separate section preserves the account table and
avoids counting unread repositories as refusals. `main()` emits a warning per
entry and keeps its refusal-based exit status. The next scheduled run reads
these repositories again; turning this recoverable omission red obscures the
work the rest of the fleet completed. The initial `/app/installations` read
still fails the job when exhausted because no sweep can proceed without it.

A read exhausted inside a hold keeps that hold and also names the repository
as not read, once per sweep. The repository still counts as served and keeps
its existing release eligibility.

A maintainer permission read for a review-body answer uses `recorded_right` so
an exhausted transient refusal is recorded like the four hold reads before the
hold text names the candidate. `test_exhausted_permission_read_warns_and_holds`,
`test_nontransient_permission_read_holds_without_warning`, and
`test_third_permission_read_answer_records_nothing` cover it.

When a repository run is refused after an exhausted hold read,
`sweep()` lists both the refusal and the collected not-read entry; a terminal
unread read still warns once for that repository.
`test_exhausted_hold_read_survives_later_refusal` and
`test_terminal_unread_after_hold_read_warns_once` cover it.

The inline `QUERY` in `.github/workflows/merge-train.yml` reads 10 open pull
requests per page; its derived `STATUS_QUERY` inherits that size. Ten pages in
`pull_pages()` preserve the 100-pull-request cap. `PULL_LIMIT` bounds row loops
independently of page count. Ten entries reduce measured payload and latency
without doubling the page count again to twenty as five-entry pages would.
No field or new constant is added to the inline rules module.

After marker and unmet linked-issue checks, `_sourcery_finding()` accepts a
no-access notice only when every non-blank line fully matches its greeting,
exact access sentence, or exact upgrade sentence. The greeting accepts user
logins and an optional `[bot]` suffix. The upgrade URL must start with
`https://app.sourcery.ai/dashboard/billing?` and contain neither whitespace nor
`)`. The access sentence must be present. Extra content retains the existing
classification; review markers and unmet linked issues take precedence.

## Alternatives

- Keep exhausted reads red: preserves the former transport-refusal policy but
  makes successful fleet work appear failed because one repository was unread.
- Halve pages on failure: adapts payload size but adds a second pagination path
  and makes cursor and cap accounting harder to verify.
- Retry more: increases outage delay and sweep budget consumption without
  reducing the payload that caused the measured failures.

## Verification

`tests/test_merge_train.py` covers the behavior through the stand-in forge:

- `test_planted_unread_repository_does_not_fail_the_fleet`: exhausted 502 and
  cut-body reads warn while the next repository in the installation lands.
- `test_third_read_answer_lands_without_not_read_entry` and
  `test_exhausted_read_retries_fail_closed`: success on attempt three versus
  exactly three failed reads with no writes.
- `test_exhausted_rest_read_retries_refuse_the_repository`,
  `test_exhausted_repositories_read_names_account_as_not_read`, and
  `test_exhausted_installations_read_still_fails_the_sweep`: REST and account
  omissions versus a fatal missing installation list.
- `test_read_500_is_not_retried`, `test_mutation_502_is_not_retried`, and
  `test_cut_mutation_answer_is_not_tried_again`: refusals stay red.
- `test_unread_error_preserves_transient_cause` and
  `test_unread_summary_keeps_only_first_error_line`: cause identity, retry
  count, error formatting, and unsigned-bot preservation.
- `test_open_pull_query_uses_small_pages_and_lands` and
  `test_pull_pages_stop_at_cap`: both query variants land through a forge that
  refuses pages above ten; pagination stops at 100.
- `test_planted_sourcery_no_access_notice_lands` and
  `test_planted_sourcery_no_access_with_other_content_holds`: both greeting
  forms land, while extra content, missing access text, and malformed URLs or
  logins hold. Existing clean-body and approval-pending cases remain covered.

The lane report records planted mutants for refusal routing, oversized pages,
removed no-access recognition, and mutation errors misclassified as unread.
